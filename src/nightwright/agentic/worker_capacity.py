"""A worker host's capacity and load (SPEC-0508): `nw worker info --json`'s `capacity` block
(`nw.worker.capacity.v1`), which the coordinator caches with the rest of `info` and
`nw workers list` / `check` render.

`source: proc` reads this host: `os.cpu_count()`, `/proc/meminfo`, `/proc/loadavg`. Under the
`slurm` executor the node's own view wins (`scontrol show node <host> --json`: cpus, memory,
what Slurm has allocated), and any Slurm CLI failure falls back to `proc` with `error` set.
Per running job (either source): its process groups' CPU (two `/proc/<pid>/stat` samples
`SAMPLE_S` apart) and resident memory (`VmRSS`). The jobs run on this host in both cases, so
`/proc` sees them; Slurm's `sstat` is not needed for a single node."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path

SCHEMA = "nw.worker.capacity.v1"
SAMPLE_S = 0.5
SLURM_TIMEOUT_S = 5.0


def _num(v):
    """A Slurm JSON number: a plain one, or `{"set": true, "number": N}` (24.05+)."""
    if isinstance(v, dict):
        return v.get("number") if v.get("set", True) and not v.get("infinite") else None
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _meminfo(root: Path) -> tuple[int | None, int | None]:
    vals = {}
    try:
        for line in (root / "meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            if key in ("MemTotal", "MemAvailable"):
                vals[key] = int(rest.split()[0]) // 1024        # kB -> MB
    except (OSError, ValueError, IndexError):
        pass
    return vals.get("MemTotal"), vals.get("MemAvailable")


def _loadavg(root: Path) -> list[float] | None:
    try:
        return [float(x) for x in (root / "loadavg").read_text().split()[:3]]
    except (OSError, ValueError):
        return None


def _stat(root: Path, pid: str) -> tuple[int, int] | None:
    """`(pgrp, utime + stime ticks)` of one process, None when it is gone."""
    try:
        raw = (root / pid / "stat").read_text()
    except OSError:
        return None
    rest = raw.rsplit(")", 1)[-1].split()
    try:                                        # fields after `comm`: state is index 0
        return int(rest[2]), int(rest[11]) + int(rest[12])
    except (IndexError, ValueError):
        return None


def _rss_mb(root: Path, pid: str) -> float:
    try:
        for line in (root / pid / "status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def _running_jobs(cfg) -> dict[str, set[int]]:
    """`{job_id: process group ids}` of the jobs whose job process runs now (`job.lock` held),
    from `proc.json` (the job process and every gate group it recorded) or `spawn.json`."""
    from . import runproc
    from . import worker as W
    from . import worker_job as WJ
    out = {}
    jd = W.jobs_dir(cfg)
    for job in sorted(jd.iterdir()) if jd.is_dir() else []:
        if not job.is_dir() or (job / "result.json").exists() or not WJ._lock_held(job):
            continue
        groups = set()
        for name in ("proc.json", "spawn.json"):
            rec = runproc.read_json(job / name) or {}
            for g in [rec, *[x for x in rec.get("gates") or [] if isinstance(x, dict)]]:
                pg = g.get("pgid") or g.get("pid")
                if isinstance(pg, int) and not isinstance(pg, bool) and pg > 0:
                    groups.add(pg)
        if groups:
            out[job.name] = groups
    return out


def _sample(root: Path, groups: dict[str, set[int]]) -> dict[str, dict[str, int]]:
    """`{job_id: {pid: ticks}}` for every process in a job's groups."""
    by_group = {pg: jid for jid, pgs in groups.items() for pg in pgs}
    out: dict[str, dict[str, int]] = {jid: {} for jid in groups}
    try:
        pids = [p for p in os.listdir(root) if p.isdigit()]
    except OSError:
        return out
    for pid in pids:
        st = _stat(root, pid)
        if st is not None and st[0] in by_group:
            out[by_group[st[0]]][pid] = st[1]
    return out


def jobs_usage(cfg, root: Path = Path("/proc"), sample_s: float = SAMPLE_S,
               groups: dict | None = None) -> list[dict]:
    """Per running job: `cpu_pct` (100 = one core) over `sample_s`, `rss_mb` (sum of `VmRSS`
    over its processes). Sampled only when a job runs."""
    groups = _running_jobs(cfg) if groups is None else groups
    if not groups:
        return []
    hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    t0, first = time.monotonic(), _sample(root, groups)
    time.sleep(sample_s)
    dt, second = max(time.monotonic() - t0, 1e-3), _sample(root, groups)
    out = []
    for jid in sorted(groups):
        ticks = sum(t - first[jid].get(pid, t) for pid, t in second[jid].items())
        out.append({"job_id": jid, "state": "running",
                    "cpu_pct": round(100.0 * ticks / hz / dt, 1),
                    "rss_mb": round(sum(_rss_mb(root, pid) for pid in second[jid]))})
    return out


def from_proc(root: Path = Path("/proc"), cpu_count=os.cpu_count) -> dict:
    total, avail = _meminfo(root)
    return {"schema": SCHEMA, "source": "proc", "cpus": cpu_count(), "mem_total_mb": total,
            "mem_available_mb": avail, "loadavg": _loadavg(root), "alloc_cpus": None,
            "alloc_mem_mb": None}


def from_slurm(host: str | None = None, run=subprocess.run) -> dict:
    """The node as Slurm sees it. Raises `RuntimeError` on any CLI or parse failure."""
    host = host or socket.gethostname().split(".")[0]
    try:
        p = run(["scontrol", "show", "node", host, "--json"], capture_output=True, text=True,
                timeout=SLURM_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"scontrol show node {host}: {e}")
    if p.returncode != 0:
        raise RuntimeError(f"scontrol show node {host} exited {p.returncode}: "
                           f"{(p.stderr or p.stdout).strip()[-300:]}")
    try:
        node = json.loads(p.stdout)["nodes"][0]
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"scontrol show node {host}: unreadable JSON: {e}")
    cpus = _num(node.get("cpus"))
    load = _num(node.get("cpu_load"))
    if not isinstance(cpus, int):
        raise RuntimeError(f"scontrol show node {host}: no cpus")
    total, alloc_mem = _num(node.get("real_memory")), _num(node.get("alloc_memory"))
    # what Slurm can still hand out (`free_mem` is the kernel's MemFree: page cache counts
    # as used there, so it reads far lower than MemAvailable; it is kept as `free_mem_mb`)
    avail = total - alloc_mem if isinstance(total, int) and isinstance(alloc_mem, int) else None
    return {"schema": SCHEMA, "source": "slurm", "cpus": cpus,
            "mem_total_mb": total, "mem_available_mb": avail,
            "free_mem_mb": _num(node.get("free_mem")),
            # Slurm reports the 1-minute load x 100
            "loadavg": [round(load / 100, 2)] if load is not None else None,
            "alloc_cpus": _num(node.get("alloc_cpus")), "alloc_mem_mb": alloc_mem}


def capacity(cfg, *, root: Path = Path("/proc"), run=subprocess.run, cpu_count=os.cpu_count,
             sample_s: float = SAMPLE_S, host: str | None = None) -> dict:
    """The `capacity` block for `nw worker info`. Never raises: a failure is `error`."""
    from . import executors as EX
    try:
        if EX.configured(cfg) == "slurm":
            try:
                out = from_slurm(host, run)
            except RuntimeError as e:
                out = {**from_proc(root, cpu_count), "error": str(e)}
        else:
            out = from_proc(root, cpu_count)
        out["jobs"] = jobs_usage(cfg, root, sample_s)
    except Exception as e:                      # pragma: no cover - info must still answer
        return {"schema": SCHEMA, "source": "proc", "error": f"{type(e).__name__}: {e}",
                "jobs": []}
    return out


def ratios(cap) -> tuple[float | None, float | None]:
    """`(load, mem)` for a list row: 1-minute load per cpu, and memory in use (Slurm:
    allocated) as a fraction of the total. None where the block cannot say."""
    if not isinstance(cap, dict):
        return None, None
    cpus = cap.get("cpus")
    la = cap.get("loadavg") or []
    load = round(la[0] / cpus, 2) if la and isinstance(cpus, int) and cpus > 0 else None
    total = cap.get("mem_total_mb")
    mem = None
    if isinstance(total, (int, float)) and total > 0:
        if cap.get("source") == "slurm" and isinstance(cap.get("alloc_mem_mb"), (int, float)):
            mem = round(cap["alloc_mem_mb"] / total, 2)
        elif isinstance(cap.get("mem_available_mb"), (int, float)):
            mem = round((total - cap["mem_available_mb"]) / total, 2)
    return load, mem
