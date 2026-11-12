"""`nw data usage` (SPEC-0580): a read-only, per-source size / count / retention report of the
agentic data dir. One `os.scandir` walk assigns every regular file to exactly one source by
`classify`; `usage(cfg)` returns the `nw.data.usage.v1` document. Retention texts interpolate the
effective settings from `disk_clean.settings` / `gate_jobs.settings`, so they cannot drift from the
sweep. Nothing here writes, touches or locks anything."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

from . import disk_clean as DC
from . import gate_jobs as GJ
from . import runner_jobs as RJ
from . import store

SCHEMA = "nw.data.usage.v1"

# (id, paths emitted as `path`, `count` unit): the stable listing order of `sources`.
SOURCES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("campaigns", ("campaigns/*/*",), "campaigns"),
    ("runs", ("campaigns/*/runs/**",), "run dirs"),
    ("streams", ("campaigns/*/runs/*/stream.jsonl",), "stream files"),
    ("gates", ("campaigns/*/gates/**",), "gate record files"),
    ("campaign-tmp", ("campaigns/*/tmp/**",), "campaigns with tmp"),
    ("sentinel", ("sentinel/**",), "sentinel log files"),
    ("gatejobs", ("gatejobs/**",), "job dirs"),
    ("remote-jobs", ("remote-jobs/**", "job-queue/**", "job-uploads/**", "job-frames/**"),
     "job records"),
    ("runner-jobs", ("runner-jobs/**",), "job records"),
    ("spec-reviews", ("reviews/**", "spec-reviews/**", "spec-reviews.jsonl"), "review files"),
    ("events", ("events.jsonl", "events.lock", "events.projector.json", "cursors/**"), "files"),
    ("runner-logs", ("runner.jsonl", "runner.log"), "files"),
    ("decisions", ("conductor/**",), "files"),
    ("mailbox", ("mailbox/**",), "inbox files"),
    ("other", ("*",), "files"),
)

_FIRST_PART = {
    "gatejobs": "gatejobs", "runner-jobs": "runner-jobs", "sentinel": "sentinel",
    "remote-jobs": "remote-jobs", "job-queue": "remote-jobs", "job-uploads": "remote-jobs",
    "job-frames": "remote-jobs", "reviews": "spec-reviews", "spec-reviews": "spec-reviews",
    "spec-reviews.jsonl": "spec-reviews", "events.jsonl": "events", "events.lock": "events",
    "events.projector.json": "events", "cursors": "events", "runner.jsonl": "runner-logs",
    "runner.log": "runner-logs", "conductor": "decisions", "mailbox": "mailbox",
}


def classify(parts: tuple[str, ...]) -> str:
    """The source id of the file at `parts` (its path below the agentic dir, split). The first
    matching rule wins: `gates/` and `tmp/` before `campaigns`; `streams` is exactly
    `campaigns/<id>/runs/<run>/stream.jsonl`; a loose file directly under `campaigns/` is `other`."""
    if parts[0] == "campaigns":
        if len(parts) < 3:
            return "other"
        if len(parts) >= 4:
            if parts[2] == "gates":
                return "gates"
            if parts[2] == "tmp":
                return "campaign-tmp"
            if parts[2] == "runs":
                return "streams" if len(parts) == 5 and parts[4] == "stream.jsonl" else "runs"
        return "campaigns"
    return _FIRST_PART.get(parts[0], "other")


def _stat(entry: os.DirEntry):
    """The one stat call of the walk (a test seam)."""
    return entry.stat(follow_symlinks=False)


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    ts = min(max(ts, -62135596800), 253402300799)     # a wild mtime clamps to year 1 / 9999
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _walk(root: str):
    """Yield `(parts, size, mtime)` of every regular file below `root` (symlinks neither followed
    nor yielded); an unreadable dir or vanished file yields `None` once per failure."""
    stack = [((), root)]
    while stack:
        rel, path = stack.pop()
        try:
            it = os.scandir(path)
        except OSError:
            if rel:                       # a missing root is just empty, not a skip
                yield None
            continue
        with it:
            while True:
                try:
                    entry = next(it)
                except StopIteration:
                    break
                except OSError:
                    yield None
                    break
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append((rel + (entry.name,), entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        st = _stat(entry)
                        yield rel + (entry.name,), st.st_size, st.st_mtime
                except OSError:
                    yield None


def _num(v) -> str:
    return str(int(v)) if float(v) == int(v) else f"{v:g}"


def _retention(sid: str, cfg) -> dict:
    dc, gj = DC.settings(cfg), GJ.settings(cfg)
    off = "sweep disabled (`nw queue disk-clean` still works)"

    def r(rule, setting, text):
        return {"rule": rule, "setting": setting, "text": text}

    if sid == "streams":
        return r("age", "agentic.disk_clean.run_days", off if not dc["enabled"] else
                 f"terminal campaigns only, older than {_num(dc['run_days'])} d "
                 "(live campaigns never)")
    if sid == "campaign-tmp":
        return r("on-terminal", "agentic.disk_clean",
                 off if not dc["enabled"] else "removed when the campaign is terminal")
    if sid == "sentinel":
        return r("age", "agentic.disk_clean.log_days", off if not dc["enabled"] else
                 f"logs older than {_num(dc['log_days'])} d, newest {DC.KEEP_NEWEST_LOGS} per "
                 "dir kept; other files forever")
    if sid == "gatejobs":                 # `nw gate run` sweeps it too, so `enabled` is moot
        return r("age", "agentic.gate_jobs.retention_days",
                 f"final/lost jobs older than {_num(gj['retention_days'])} d")
    if sid == "remote-jobs":
        return r("on-campaign-end", "remote_jobs.prune",
                 "final records (and pull-job frames/uploads) dropped when their campaign "
                 "ends; live and queued ones kept")
    if sid == "runner-jobs":
        return r("age", None, f"finished or cancelled jobs removed after {_num(RJ.KEEP_S / 86400)} d")
    return r("forever", None, "kept forever (no sweep)")


def _read_state(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f).get("state")
        return st if isinstance(st, str) and st else "UNKNOWN"
    except (OSError, ValueError, AttributeError):
        return "UNKNOWN"


def usage(cfg, *, now: float | None = None) -> dict:
    """The `nw.data.usage.v1` document for `cfg`'s agentic dir. Read-only."""
    now = time.time() if now is None else now
    root = str(store.agentic_dir(cfg))
    acc = {sid: {"bytes": 0, "files": 0, "old": None, "new": None, "keys": set(), "n": 0}
           for sid, _, _ in SOURCES}
    camp_bytes: dict[str, int] = {}
    states: dict[str, str] = {}
    skipped = 0
    for item in _walk(root):
        if item is None:
            skipped += 1
            continue
        parts, size, mtime = item
        sid = classify(parts)
        a = acc[sid]
        a["bytes"] += size
        a["files"] += 1
        a["old"] = mtime if a["old"] is None else min(a["old"], mtime)
        a["new"] = mtime if a["new"] is None else max(a["new"], mtime)
        if sid == "campaigns":
            camp_bytes[parts[1]] = camp_bytes.get(parts[1], 0) + size
            if parts[2:] == ("state.json",):
                states[parts[1]] = _read_state(os.path.join(root, *parts))
        elif sid in ("streams", "runs"):
            if len(parts) >= 5:                 # a run dir: campaigns/<id>/runs/<run>/<file>
                acc["runs"]["keys"].add((parts[1], parts[3]))
        elif sid == "campaign-tmp":
            a["keys"].add(parts[1])
        elif sid == "sentinel":
            a["n"] += len(parts) >= 3 and parts[1].endswith(".logs")
        elif sid in ("gatejobs", "runner-jobs"):
            if len(parts) >= 3:
                a["keys"].add(parts[1])
        elif sid == "remote-jobs":
            a["n"] += len(parts) == 2 and parts[0] == "remote-jobs" and parts[1].endswith(".json")

    rows = []
    for sid, paths, unit in SOURCES:
        a = acc[sid]
        count = {"campaigns": len(states), "runs": len(a["keys"]), "campaign-tmp": len(a["keys"]),
                 "gatejobs": len(a["keys"]), "runner-jobs": len(a["keys"]),
                 "sentinel": a["n"], "remote-jobs": a["n"]}.get(sid, a["files"])
        row = {"id": sid, "path": list(paths), "bytes": a["bytes"], "files": a["files"],
               "count": count, "count_unit": unit, "oldest": _iso(a["old"]),
               "newest": _iso(a["new"]), "retention": _retention(sid, cfg)}
        if sid == "campaigns":
            by_state: dict[str, dict] = {}
            for cid, st in states.items():
                b = by_state.setdefault(st, {"count": 0, "bytes": 0})
                b["count"] += 1
                b["bytes"] += camp_bytes.get(cid, 0)
            row["by_state"] = dict(sorted(by_state.items()))
        rows.append(row)
    return {"schema": SCHEMA, "root": root, "generated_at": _iso(now), "skipped": skipped,
            "total": {"bytes": sum(r["bytes"] for r in rows),
                      "files": sum(r["files"] for r in rows)},
            "sources": rows}


def human_size(n: int) -> str:
    """IEC units: `B`, `KiB`, `MiB`, `GiB`, `TiB`."""
    if n < 1024:
        return f"{n} B"
    v = float(n)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        v /= 1024
        if v < 1024 or unit == "TiB":
            return f"{v:.1f} {unit}"
    return f"{v:.1f} TiB"            # pragma: no cover


def render_rows(doc: dict) -> list[dict]:
    """The report rows for the text form, biggest first (ties keep the table order)."""
    return sorted(doc["sources"], key=lambda r: -r["bytes"])
