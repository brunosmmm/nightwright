"""`nw gate stats` (SPEC-0514): per (project, gate) measured usage over the gate records of
campaigns and standalone gate jobs, and a suggested per-gate request.

Percentiles are nearest-rank (p95 of a handful of runs is the largest: the safe side). The
suggestion is `cpus = ceil(p95 effective cpus)` (`cpu_s / wall_s`) and `mem = ceil(p95
max_rss_mb x 1.25 / 256) x 256` MB. `over`: the request is more than 50 % above the suggestion
(cpus, or memory when the evidence is a whole-cgroup measurement); `under`: p95 peak memory is
above the request (OOM risk). Rows measured with `wait4` (`rss_scope: max_process`, the
largest single process) under-report a process tree and a `job_cgroup_cumulative` one (the
box's cgroup files are root-owned: no per-gate reset) is the job's peak so far, so their memory suggestion is marked
approximate (`mem_exact: false`) and never flags `over`."""
from __future__ import annotations

import glob
import json
import math
import os
import re
import time
from datetime import datetime
from pathlib import Path

from . import store
from . import gate_jobs as J

SCHEMA = "nw.gate.stats.v1"
HEADROOM = 1.25
MEM_STEP_MB = 256
OVER_FACTOR = 1.5
EXACT_SCOPES = ("job_cgroup",)      # a cumulative job peak is only an upper bound
_SINCE_RE = re.compile(r"^(\d+)([smhdw])\Z")
_UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


def parse_since(text: str) -> float:
    """`14d` / `36h` / `90m` -> seconds. ValueError for anything else."""
    m = _SINCE_RE.match(str(text).strip())
    if not m or int(m.group(1)) < 1:
        raise ValueError(f"--since must look like 14d, 36h or 90m, not {text!r}")
    return int(m.group(1)) * _UNIT_S[m.group(2)]


def percentile(values: list, p: float):
    """Nearest-rank percentile (`p` in 0..100) of a non-empty list; None for an empty one."""
    vals = sorted(values)
    if not vals:
        return None
    return vals[max(1, math.ceil(p / 100 * len(vals))) - 1]


def _epoch(rec: dict, path: str) -> float:
    try:
        return datetime.fromisoformat(rec["started_at"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return os.path.getmtime(path)


def _tests(summary) -> int | None:
    if not isinstance(summary, dict) or summary.get("format") != "pytest":
        return None
    return sum(int(summary.get(k) or 0) for k in ("passed", "failed", "errors", "skipped"))


def _row(rec: dict, path: str, project, source: str, worker) -> dict | None:
    res = rec.get("resources")
    if not isinstance(res, dict) or not isinstance(res.get("used"), dict):
        return None
    used, req = res["used"], res.get("request") if isinstance(res.get("request"), dict) else {}
    start = _epoch(rec, path)
    wall = rec.get("duration_s")
    slurm = res.get("slurm") if isinstance(res.get("slurm"), dict) else None
    return {"project": project, "gate": rec.get("name"), "source": source, "worker": worker,
            "start": datetime.fromtimestamp(start).astimezone().isoformat(timespec="seconds"),
            "end": datetime.fromtimestamp(start + wall).astimezone().isoformat(timespec="seconds")
            if isinstance(wall, (int, float)) else None,
            "_t": start, "ok": rec.get("ok"), "tests": _tests(rec.get("summary")),
            "wall_s": used.get("wall_s") if used.get("wall_s") is not None else wall,
            "cpu_s": used.get("cpu_s"), "max_rss_mb": used.get("max_rss_mb"),
            "rss_scope": used.get("rss_scope"), "request": req or None,
            "slurm_state": (slurm or {}).get("State")}


def collect(cfg, since_s: float | None = None, project: str | None = None,
            now: float | None = None) -> list[dict]:
    """Every gate record with `resources` in the window, oldest first."""
    now = time.time() if now is None else now
    rows = []
    camp_proj: dict[str, str | None] = {}
    for path in glob.glob(str(store.campaigns_dir(cfg) / "*" / "gates" / "*.json")):
        if path.endswith(".report.json"):
            continue
        cdir = Path(path).parent.parent
        if cdir.name not in camp_proj:
            try:
                camp_proj[cdir.name] = json.loads((cdir / "state.json").read_text()).get("project")
            except (OSError, ValueError):
                camp_proj[cdir.name] = None
        row = _read(path, camp_proj[cdir.name], "campaign", None)
        if row:
            rows.append(row)
    for path in glob.glob(str(J.root(cfg) / "*" / "gates" / "*.json")):
        if path.endswith(".report.json"):
            continue
        try:
            job = json.loads((Path(path).parent.parent / "job.json").read_text())
        except (OSError, ValueError):
            job = {}
        row = _read(path, job.get("project"), "gatejob", job.get("worker"))
        if row:
            rows.append(row)
    if since_s is not None:
        rows = [r for r in rows if r["_t"] >= now - since_s]
    if project:
        rows = [r for r in rows if r["project"] == project]
    return sorted(rows, key=lambda r: r["_t"])


def _read(path, project, source, worker):
    try:
        rec = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict) or not isinstance(rec.get("name"), str):
        return None
    return _row(rec, path, project, source, worker)


def suggest(p95_cpus, p95_rss_mb) -> dict:
    """`{cpus, mem_mb}` from the p95 effective cpus and peak memory (None stays None)."""
    return {"cpus": max(1, math.ceil(p95_cpus - 1e-9)) if p95_cpus is not None else None,
            "mem_mb": max(MEM_STEP_MB, math.ceil(p95_rss_mb * HEADROOM / MEM_STEP_MB)
                          * MEM_STEP_MB) if p95_rss_mb is not None else None}


def _pair(values):
    vals = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return (percentile(vals, 50), percentile(vals, 95)) if vals else (None, None)


def _r(v, nd=1):
    return None if v is None else round(v, nd)


def summarize(rows: list[dict]) -> list[dict]:
    """One entry per (project, gate), sorted by project then gate."""
    groups: dict[tuple, list] = {}
    for r in rows:
        groups.setdefault((r["project"], r["gate"]), []).append(r)
    out = []
    for (proj, gate), rs in sorted(groups.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
        eff = [r["cpu_s"] / r["wall_s"] for r in rs
               if isinstance(r["cpu_s"], (int, float)) and isinstance(r["wall_s"], (int, float))
               and r["wall_s"] > 0]
        w50, w95 = _pair([r["wall_s"] for r in rs])
        c50, c95 = _pair(eff)
        m50, m95 = _pair([r["max_rss_mb"] for r in rs])
        scopes = {r["rss_scope"] for r in rs if r["max_rss_mb"] is not None}
        exact = bool(scopes) and scopes <= set(EXACT_SCOPES)
        scope = (next(iter(scopes)) if len(scopes) == 1 else "mixed") if scopes else None
        req = next((r["request"] for r in reversed(rs) if r["request"]), None) or {}
        sug = suggest(c95, m95)
        flags = []
        if req.get("mem_mb") and m95 is not None and m95 > req["mem_mb"]:
            flags.append("under")
        over = (req.get("cpus") and sug["cpus"] and req["cpus"] > OVER_FACTOR * sug["cpus"]) or \
               (exact and req.get("mem_mb") and sug["mem_mb"]
                and req["mem_mb"] > OVER_FACTOR * sug["mem_mb"])
        if over and "under" not in flags:
            flags.append("over")
        out.append({
            "project": proj, "gate": gate, "n": len(rs),
            "wall_s": {"p50": _r(w50), "p95": _r(w95)},
            "cpus": {"p50": _r(c50, 2), "p95": _r(c95, 2)},
            "max_rss_mb": {"p50": _r(m50), "p95": _r(m95)}, "rss_scope": scope,
            "request": {"cpus": req.get("cpus"), "mem_mb": req.get("mem_mb")},
            "suggested": {**sug, "mem_exact": exact}, "flags": flags,
            "oom": sum(1 for r in rs if r["slurm_state"] == "OUT_OF_MEMORY")})
    return out


def public_rows(rows: list[dict]) -> list[dict]:
    return [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]


def fmt_mem(mb) -> str:
    return "-" if mb is None else (f"{mb / 1024:g}G" if mb >= 1024 and mb % 256 == 0
                                   else f"{mb:g}M")
