"""SPEC-0515: was a gate inside a Slurm job killed by the OOM killer?

Slurm's cgroup (v2) enforces the job's `--mem`; the kernel then SIGKILLs a process of the job
and counts it in `memory.events` `oom_kill`. A gate whose process ended by SIGKILL (exit -9, or
137 through a shell) while that counter rose is an `oom` infra outcome, not a red gate.

The counter is read from the calling process's own cgroup (`/proc/self/cgroup`, the job's
`.../step_batch/user/task_0` under Slurm 26.05) and each ancestor up to the job's own level (the
directory right under `slurmstepd.scope`), where the job's limit sits; the largest value wins
(`memory.events` counts descendants, unless the hierarchy is mounted `memory_localevents`).
Only inside a Slurm job (`SLURM_JOB_ID` set): elsewhere a SIGKILL is never classified."""
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

CGROUP_ROOT = Path("/sys/fs/cgroup")
PROC_CGROUP = Path("/proc/self/cgroup")
# a stand-in cgroup tree for the tests (a job process runs in a subprocess there)
ROOT_ENV, PROC_ENV = "NW_CGROUP_ROOT", "NW_PROC_CGROUP"
SLURM_SCOPE = "slurmstepd.scope"
KILLED = (-9, 137)                  # SIGKILL as a subprocess return code / through a shell


def _dirs() -> list[Path]:
    root = Path(os.environ.get(ROOT_ENV) or CGROUP_ROOT)
    try:
        lines = Path(os.environ.get(PROC_ENV) or PROC_CGROUP).read_text(
            encoding="utf-8").splitlines()
    except OSError:
        return []
    rel = next((ln.split(":", 2)[2] for ln in lines if ln.startswith("0::")), None)
    if not rel:
        return []
    parts = [p for p in rel.strip("/").split("/") if p]
    stop = len(parts)
    if SLURM_SCOPE in parts:        # up to the job's own level, never other jobs' ancestors
        stop = min(len(parts), parts.index(SLURM_SCOPE) + 2)
    else:
        return [root.joinpath(*parts)]
    return [root.joinpath(*parts[:i]) for i in range(len(parts), stop - 1, -1)]


def _read(d: Path) -> int | None:
    try:
        text = (d / "memory.events").read_text(encoding="utf-8")
    except OSError:
        return None
    for ln in text.splitlines():
        key, _, val = ln.partition(" ")
        if key == "oom_kill" and val.strip().isdigit():
            return int(val.strip())
    return None


def oom_kills() -> int | None:
    """The job cgroup's `oom_kill` count, or None outside a Slurm job or when unreadable."""
    if not os.environ.get("SLURM_JOB_ID"):
        return None
    vals = [v for v in (_read(d) for d in _dirs()) if v is not None]
    return max(vals) if vals else None


def classify(result, before: int | None):
    """`result` (a `GateResult`) with `infra="oom"` when its process was SIGKILLed while the
    counter rose from `before`; else unchanged."""
    if before is None or result.ok or result.interrupted or result.exit_code not in KILLED:
        return result
    after = oom_kills()
    if after is not None and after > before:
        return replace(result, infra="oom")
    return result
