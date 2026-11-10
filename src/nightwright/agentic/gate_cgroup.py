"""SPEC-0514: what a gate used, measured on the job's cgroup (cgroup v2).

Inside a Slurm job the cgroup covers every process of the gate (pytest-xdist workers
included), where `wait4` only knows the largest single process. `Meter.begin()` resets the
cgroup's `memory.peak` (a per-fd reset: write `0` to the open file) and reads `cpu.stat`
`usage_usec`; `Meter.end()` reads both again. Missing files (old kernel, the local executor)
give `None`: the caller keeps SPEC-0504's `wait4` numbers. A refused reset (on the box the
cgroup files are root-owned, so a job cannot reset them) records the job's cumulative peak and
says so (`rss_scope: job_cgroup_cumulative`): exact when the gate raised it (`rss_bound`
absent), else only an upper bound (`rss_bound: "upper"`: the peak of an earlier phase)."""
from __future__ import annotations

import os
from pathlib import Path

CGROUP_ROOT = "/sys/fs/cgroup"
SCOPE = "job_cgroup"
SCOPE_CUMULATIVE = "job_cgroup_cumulative"


def own_cgroup(proc_file: str = "/proc/self/cgroup") -> str | None:
    """The `0::` (v2) path of this process, e.g. `/system.slice/slurmstepd.scope/job_7/...`."""
    try:
        for line in Path(proc_file).read_text().splitlines():
            if line.startswith("0::"):
                return line[3:].strip() or None
    except OSError:
        pass
    return None


def job_cgroup(environ=None, root: str | None = None, proc_file: str = "/proc/self/cgroup"
               ) -> Path | None:
    """This job's cgroup directory: the highest ancestor of this process's cgroup named
    `job_<SLURM_JOB_ID>`; else (the box's layout is `.../<random id>/step_batch/user/task_0`)
    the directory above the first `step_*` one; else this process's own cgroup. None outside
    a Slurm job or without cgroup v2 files."""
    environ = os.environ if environ is None else environ
    jid = str(environ.get("SLURM_JOB_ID") or "").strip()
    if not jid:
        return None
    rel = own_cgroup(proc_file)
    if not rel:
        return None
    parts = [p for p in rel.split("/") if p]
    chosen = len(parts)
    for i, p in enumerate(parts):
        if p == f"job_{jid}":
            chosen = i + 1
            break
    else:
        for i, p in enumerate(parts):
            if p.startswith("step_") and i > 0:
                chosen = i
                break
    for n in dict.fromkeys((chosen, len(parts))):       # the job dir, else the process's own
        d = Path(root or CGROUP_ROOT).joinpath(*parts[:n])
        if (d / "memory.peak").is_file() and (d / "cpu.stat").is_file():
            return d
    return None


def _usage_usec(d: Path) -> int | None:
    try:
        for line in (d / "cpu.stat").read_text().splitlines():
            k, _, v = line.partition(" ")
            if k == "usage_usec":
                return int(v)
    except (OSError, ValueError):
        pass
    return None


class Meter:
    """One gate's measurement window over a cgroup directory."""

    def __init__(self, cgroup: Path | None):
        self.dir = cgroup
        self._fd = None
        self._cpu0: int | None = None
        self._reset = False
        self._peak0: int | None = None

    @classmethod
    def for_job(cls, environ=None, **kw) -> "Meter":
        return cls(job_cgroup(environ, **kw))

    def begin(self) -> None:
        if self.dir is None:
            return
        try:
            self._fd = open(self.dir / "memory.peak", "r+")
        except OSError:
            try:
                self._fd = open(self.dir / "memory.peak")
            except OSError:
                self.dir = None
                return
        try:
            self._fd.write("0")
            self._fd.flush()
            self._reset = True
        except OSError:
            self._reset = False
            try:
                self._fd.seek(0)
                self._peak0 = int(self._fd.read().strip())
            except (OSError, ValueError):
                self._peak0 = None
        self._cpu0 = _usage_usec(self.dir)

    def end(self, wall_s: float) -> dict | None:
        """`{cpu_s, max_rss_mb, wall_s, rss_scope}`, or None when nothing could be read."""
        fd, self._fd = self._fd, None
        if self.dir is None or fd is None:
            return None
        try:
            fd.seek(0)
            peak = int(fd.read().strip())
        except (OSError, ValueError):
            return None
        finally:
            try:
                fd.close()
            except OSError:
                pass
        cpu1 = _usage_usec(self.dir)
        if self._cpu0 is None or cpu1 is None:
            return None
        out = {"cpu_s": round(max(cpu1 - self._cpu0, 0) / 1e6, 3),
               "max_rss_mb": round(peak / 2**20, 1),
               "rss_scope": SCOPE if self._reset else SCOPE_CUMULATIVE, "wall_s": wall_s}
        if not self._reset and (self._peak0 is None or peak <= self._peak0):
            out["rss_bound"] = "upper"      # the gate did not exceed the job's earlier peak
        return out
