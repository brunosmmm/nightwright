"""Self-cleaning disk sweep (SPEC-0524): bounded retention for the leftovers of finished work.

Three sources grew without bound: finished campaigns' `runs/<n>-<persona>/stream.jsonl` (the
replay log, the bulk of `<agentic>/campaigns`), the full-suite sentinel's `<name>.logs/`, and
standalone gate-job records (SPEC-0381, swept only when someone ran `nw gate run`). The sweep
removes only what is finished and old: terminal campaigns' `tmp/` and replay logs, sentinel logs
older than the window (the newest `KEEP_NEWEST_LOGS` files always stay so the `NNN-` sequence
survives), and final gate jobs past `agentic.gate_jobs.retention_days`. Records that audit a
run (`run.json`, `result.json`, `request.json`, `prompt.md`, ledger, state) are never touched, and
neither is any live campaign. Settings: `agentic.disk_clean.{enabled,run_days,log_days,interval_s}`."""
from __future__ import annotations

import math
import time
from pathlib import Path

from . import gate_jobs as J
from . import store

DEFAULTS = {"enabled": True, "run_days": 14, "log_days": 14, "interval_s": 3600}
KEEP_NEWEST_LOGS = 40
STAMP = ".disk-clean"


def settings(cfg) -> dict:
    """`DEFAULTS` overlaid with `agentic.disk_clean`; a bad value falls back to its default."""
    raw = ((cfg.get("agentic") or {}).get("disk_clean")) or {}
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULTS)
    if isinstance(raw.get("enabled"), bool):
        out["enabled"] = raw["enabled"]
    for k in ("run_days", "log_days", "interval_s"):
        v = raw.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0:
            out[k] = v
    return out


def _unlink(p: Path) -> int:
    try:
        n = p.stat().st_size
        p.unlink()
        return n
    except OSError:
        return 0


def _sweep_campaigns(cfg, cutoff: float, out: dict) -> None:
    for st in store.list_campaigns(cfg, include_terminal=True):
        if st.get("state") not in store.TERMINAL_STATES or not st.get("id"):
            continue
        cdir = store.campaign_dir(cfg, st["id"])
        tmp = store.campaign_tmp(cfg, st["id"])
        if tmp.exists() or tmp.is_symlink():
            out["bytes"] += _du(tmp)
            store.clear_campaign_tmp(cfg, st["id"])
            out["tmp_dirs"] += 1
        for f in (cdir / "runs").glob("*/stream.jsonl"):
            try:
                if not f.is_symlink() and f.stat().st_mtime < cutoff:
                    out["bytes"] += _unlink(f)
                    out["streams"] += 1
            except OSError:
                continue


def _du(p: Path) -> int:
    total = 0
    for f in ([p] if p.is_file() else p.rglob("*")):
        try:
            if f.is_file() and not f.is_symlink():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def _sweep_sentinel_logs(cfg, cutoff: float, out: dict) -> None:
    for logs in (store.agentic_dir(cfg) / "sentinel").glob("*.logs"):
        files = []
        for f in logs.iterdir():
            try:
                if f.is_file() and not f.is_symlink():
                    files.append((f.stat().st_mtime, f))
            except OSError:
                pass
        files.sort(key=lambda t: t[0], reverse=True)
        for mtime, f in files[KEEP_NEWEST_LOGS:]:
            if mtime < cutoff:
                out["bytes"] += _unlink(f)
                out["logs"] += 1


def sweep(cfg, *, now: float | None = None, force: bool = False,
          run_days: float | None = None) -> dict:
    """Run one sweep; `{ran, bytes, tmp_dirs, streams, logs, gate_jobs}`. Rate-limited to one
    per `interval_s` (a stamp file in the agentic dir) unless `force`; never raises."""
    now = time.time() if now is None else now
    s = settings(cfg)
    out = {"ran": False, "bytes": 0, "tmp_dirs": 0, "streams": 0, "logs": 0, "gate_jobs": 0}
    if not s["enabled"] and not force:
        return out
    stamp = store.agentic_dir(cfg) / STAMP
    try:
        if not force and now - stamp.stat().st_mtime < s["interval_s"]:
            return out
    except OSError:
        pass
    out["ran"] = True
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
        rd = s["run_days"] if run_days is None else run_days
        _sweep_campaigns(cfg, now - rd * 86400, out)
        _sweep_sentinel_logs(cfg, now - s["log_days"] * 86400, out)
        out["gate_jobs"] = len(J.gc(cfg, now))
    except Exception as e:  # a sweep must never break the runner tick
        out["error"] = f"{type(e).__name__}: {e}"
    return out
