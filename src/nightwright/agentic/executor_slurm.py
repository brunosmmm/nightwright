"""The Slurm executor (SPEC-0510), behind SPEC-0503's seam: a worker job runs as a Slurm batch job.

Opt-in only: a worker host uses it when its own config says `agentic.workers.executor: slurm`
(the default stays `local`; the coordinator never chooses). A job records the executor that
started it in `job.json`, so a host switched back to `local` still reads, cancels and finishes
its Slurm jobs through this module.

- `start`: after the claim and spool, `sbatch --parsable` runs `<own nw> worker run-job <jobdir>`,
  named by the nw job id, sized by SPEC-0504's request (`payload.resources`). The Slurm job id
  goes to `spawn.json` and `job.json` (`slurm_job_id`).
- `state`: Slurm's view (`squeue --json`, then `scontrol show job --json`, then `sacct --json`)
  mapped to nw's states. A PENDING job appends `slot_wait` frames carrying Slurm's reason; a job
  that ended in Slurm without `result.json` gets one final frame written by the reader.
- `cancel`: the `cancel` file plus a TERM to the batch step (`scancel --batch --signal=TERM`), so
  the job ends its gates and reports `done` interrupted; a pending job is `scancel`ed outright,
  and a running one that has not ended `cancel_grace_s` later is `scancel`ed too (the backstop).
- `host_slot`: a no-op (the allocation is the admission).
- `run_inline` (sync `gate-job`): claim + spool + submit through `worker_job.submit`, then follow
  `frames.jsonl` to stdout; stdin EOF or a signal cancels."""
from __future__ import annotations

import fcntl
import io
import json
import math
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import runproc
from . import worker as W
from .store import now_iso

NAME = "slurm"
DEFAULTS = {"conf": None, "partition": None, "bin_dir": None, "time_margin_s": 300,
            "max_time_s": 14400, "cache_s": 2.0, "call_timeout_s": 30.0, "cancel_grace_s": 60.0}
PENDING = frozenset({"PENDING", "REQUEUED", "REQUEUE_HOLD", "REQUEUE_FED", "RESV_DEL_HOLD"})
RUNNING = frozenset({"CONFIGURING", "RUNNING", "COMPLETING", "SUSPENDED", "STAGE_OUT",
                     "RESIZING", "SIGNALING", "POWER_UP_NODE", "STOPPED"})
# how long a sync `gate-job` keeps following after it cancelled (the job's `done` interrupted)
FOLLOW_AFTER_CANCEL_S = 150.0
# how long `run-job` waits for `job.lock` (submit or a reader may hold it for a moment)
LOCK_WAIT_S = 60.0
WAIT_FILE = "slurm_wait.json"           # the last `slot_wait` frame a reader appended
KILL_FILE = "slurm_scancel.json"        # the backstop `scancel` was sent
CHECK_SCHEMA = "nw.worker.slurm_check.v1"


class SlurmUnavailable(Exception):
    """A Slurm CLI could not answer (controller down, binary missing, timeout)."""


def settings(cfg) -> dict:
    """`agentic.slurm` over `DEFAULTS` (a malformed value keeps its default)."""
    raw = ((cfg or {}).get("agentic") or {}).get("slurm")
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULTS)
    for key in ("conf", "partition", "bin_dir"):
        v = raw.get(key)
        if isinstance(v, str) and v.strip():
            out[key] = v.strip()
    for key in ("time_margin_s", "max_time_s", "cache_s", "call_timeout_s", "cancel_grace_s"):
        v = raw.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) \
                and v >= 0:
            out[key] = v
    return out


def _exe(sett: dict, name: str) -> str:
    return str(Path(sett["bin_dir"]) / name) if sett.get("bin_dir") else name


def _env(sett: dict, extra: dict | None = None) -> dict:
    env = {**os.environ, **(extra or {})}
    if sett.get("conf"):
        env["SLURM_CONF"] = sett["conf"]
    return env


def _run(sett: dict, name: str, *args, env: dict | None = None,
         timeout: float | None = None) -> subprocess.CompletedProcess:
    """One Slurm CLI call, bounded; `SlurmUnavailable` when it cannot run at all."""
    argv = [_exe(sett, name), *map(str, args)]
    try:
        return subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              env=env or _env(sett),
                              timeout=timeout or sett["call_timeout_s"] or None)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SlurmUnavailable(f"{name}: {e}")


def _tail(p: subprocess.CompletedProcess) -> str:
    lines = (p.stderr or p.stdout or "").strip().splitlines()
    return lines[-1] if lines else f"exit {p.returncode}"


# ---- reading Slurm's view --------------------------------------------------------------------

def _num(v):
    """Slurm's JSON numbers come as `{set, infinite, number}` (or plain)."""
    if isinstance(v, dict):
        return v.get("number") if v.get("set", True) and not v.get("infinite") else None
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _job_fields(j: dict, source: str) -> dict:
    """The fields nw uses of one `squeue`/`scontrol`/`sacct` JSON job record."""
    raw = j.get("job_state")
    if raw is None and isinstance(j.get("state"), dict):    # sacct: state.current
        raw = j["state"].get("current")
    states = [raw] if isinstance(raw, str) else [s for s in raw or [] if isinstance(s, str)]
    reason = j.get("state_reason")
    if reason is None and isinstance(j.get("state"), dict):
        reason = j["state"].get("reason")
    if any(s in RUNNING for s in states):
        base = next(s for s in states if s in RUNNING)
    elif any(s in PENDING for s in states):
        base = "PENDING"
    else:
        base = states[0] if states else "UNKNOWN"
    ec = j.get("exit_code") if isinstance(j.get("exit_code"), dict) else {}
    sig = (ec.get("signal") or {}).get("id") if isinstance(ec.get("signal"), dict) else None
    return {"state": base, "states": states, "reason": reason if isinstance(reason, str) else None,
            "exit_code": _num(ec.get("return_code")), "signal": _num(sig),
            "restart_cnt": j.get("restart_cnt"), "name": j.get("name"),
            "cpus": _num(j.get("cpus_per_task")), "mem_mb": _num(j.get("memory_per_node")),
            "source": source}


def _jobs_of(p: subprocess.CompletedProcess) -> list | None:
    try:
        doc = json.loads(p.stdout or "")
    except ValueError:
        return None
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    return [j for j in jobs if isinstance(j, dict)] if isinstance(jobs, list) else None


_INVALID_RE = re.compile(r"invalid job id", re.I)
_ACCT_OFF_RE = re.compile(r"accounting storage is disabled|slurmdbd", re.I)
_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()


def lookup(sett: dict, jid: int) -> dict | None:
    """Slurm's view of job `jid` (`_job_fields`), or None when Slurm no longer knows it (past
    `MinJobAge`, no accounting). Order: `squeue`, `scontrol show job`, `sacct`. Raises
    `SlurmUnavailable` when the controller cannot be asked. Cached `cache_s` per job id."""
    key = (sett.get("conf"), sett.get("bin_dir"), int(jid))
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None and now - hit[0] < sett["cache_s"]:
            return hit[1]
    out = _lookup(sett, int(jid))
    with _CACHE_LOCK:
        _CACHE[key] = (now, out)
    return out


def forget(jid=None) -> None:
    """Drop cached lookups (all, or one job's): after a cancel the next read asks again."""
    with _CACHE_LOCK:
        for key in [k for k in _CACHE if jid is None or k[2] == int(jid)]:
            _CACHE.pop(key, None)


def _lookup(sett: dict, jid: int) -> dict | None:
    p = _run(sett, "squeue", "--json", "-j", jid)
    if p.returncode == 0:
        jobs = _jobs_of(p)
        if jobs is None:
            raise SlurmUnavailable(f"squeue: unreadable JSON: {_tail(p)}")
        mine = [j for j in jobs if j.get("job_id") == jid]
        if mine:
            return _job_fields(mine[0], "squeue")
    elif not _INVALID_RE.search(p.stderr or ""):
        raise SlurmUnavailable(f"squeue: {_tail(p)}")
    p = _run(sett, "scontrol", "show", "job", jid, "--json")
    if p.returncode == 0:
        jobs = _jobs_of(p)
        mine = [j for j in jobs or [] if j.get("job_id") == jid]
        if mine:
            return _job_fields(mine[0], "scontrol")
    elif not _INVALID_RE.search(p.stderr or ""):
        raise SlurmUnavailable(f"scontrol: {_tail(p)}")
    p = _run(sett, "sacct", "--json", "-j", jid)     # only answers with slurmdbd (SPEC-0514)
    if p.returncode == 0:
        mine = [j for j in _jobs_of(p) or [] if j.get("job_id") == jid]
        if mine:
            return _job_fields(mine[-1], "sacct")
    return None


_LICENSES: dict = {}


def licenses(sett: dict) -> dict[str, int]:
    """The licenses `scontrol show licenses` defines (`{name: total}`), once per process."""
    key = (sett.get("conf"), sett.get("bin_dir"))
    if key not in _LICENSES:
        try:
            p = _run(sett, "scontrol", "show", "licenses")
        except SlurmUnavailable:
            return {}
        if p.returncode != 0:
            return {}
        out = {}
        for m in re.finditer(r"LicenseName=(\S+)(?:\s+Total=(\d+))?", p.stdout or ""):
            out[m.group(1)] = int(m.group(2)) if m.group(2) else 0
        _LICENSES[key] = out
    return _LICENSES[key]


_LIMITS: dict = {}


def limits(sett: dict) -> dict:
    """What one job can ever get in the target partition (`sett["partition"]`, else Slurm's
    default one): `{partition, cpus, mem_mb, time_min}`, any of them None when Slurm sets no
    bound. `cpus`: the partition's `MaxCPUsPerNode`, capped by the largest node's usable cpus
    (`effective_cpus`: CoreSpecCount taken off); `mem_mb`: the partition's `MaxMemPerNode`,
    capped by the largest node's `RealMemory - MemSpecLimit`; `time_min`: its `MaxTime`. Read
    once per process from `scontrol show partition --json` and `scontrol show node --json`;
    `{}` when Slurm cannot be asked (nothing is clamped then)."""
    key = (sett.get("conf"), sett.get("bin_dir"), sett.get("partition"))
    if key in _LIMITS:
        return _LIMITS[key]
    try:
        pp = _run(sett, "scontrol", "show", "partition", "--json")
        pn = _run(sett, "scontrol", "show", "node", "--json")
        parts = json.loads(pp.stdout).get("partitions") if pp.returncode == 0 else None
        nodes = json.loads(pn.stdout).get("nodes") if pn.returncode == 0 else None
    except (SlurmUnavailable, ValueError, AttributeError):
        return {}
    if not isinstance(parts, list) or not isinstance(nodes, list):
        return {}
    want = sett.get("partition")
    part = next((p for p in parts if isinstance(p, dict) and (
        p.get("name") == want if want else "DEFAULT" in (p.get("partition") or {}).get("flags", [])
        or "DEFAULT" in (p.get("flags") or []))), None)
    if part is None:
        return {}
    mx = part.get("maximums") or {}
    p_cpus = _num(mx.get("cpus_per_node"))
    p_mem = _num(mx.get("partition_memory_per_node"))
    p_time = _num(mx.get("time"))
    mine = [n for n in nodes if isinstance(n, dict) and part.get("name") in (n.get("partitions") or [])]
    n_cpus = max((_num(n.get("effective_cpus")) or _num(n.get("cpus")) or 0 for n in mine),
                 default=0)
    n_mem = max(((_num(n.get("real_memory")) or 0) - (_num(n.get("specialized_memory")) or 0)
                 for n in mine), default=0)
    cpus = min([v for v in (p_cpus, n_cpus) if v]) if (p_cpus or n_cpus) else None
    mem = min([v for v in (p_mem, n_mem) if v and v > 0]) if ((p_mem or 0) > 0 or n_mem > 0) \
        else None
    out = {"partition": part.get("name"), "cpus": cpus, "mem_mb": mem,
           "time_min": p_time if p_time else None}
    _LIMITS[key] = out
    return out


# ---- submit ----------------------------------------------------------------------------------

def request(cfg, h: dict) -> dict:
    """The job's request: `payload.resources` (SPEC-0504), else the defaults for its gates
    (an older coordinator) or a persona job; `time_s` defaults to the header's `timeout_s`."""
    from . import gate_resources as GR
    payload = h.get("payload") or {}
    req = payload.get("resources")
    if not isinstance(req, dict):
        req = GR.persona_request(cfg) if h.get("kind") == "persona" \
            else GR.job_request(payload.get("gates") or [], cfg, time_s=h.get("timeout_s") or 0)
    time_s = req.get("time_s") or h.get("timeout_s") or 0
    return {"cpus": int(req.get("cpus") or 1), "mem_mb": int(req.get("mem_mb") or 1024),
            "licenses": dict(req.get("licenses") or {}), "time_s": int(time_s)}


def sbatch_argv(cfg, sett: dict, job: Path, h: dict, nw: list[str]) -> tuple[list[str], list]:
    """`(sbatch argv after the executable, warnings)` for job dir `job` with header `h`."""
    req = request(cfg, h)
    warnings = []
    lim = limits(sett)
    where = f"partition {lim.get('partition')}" if lim.get("partition") else "the partition"
    # a request no node of the partition can ever hold would stay PENDING forever: clamp it
    # to what the partition and its nodes allow (the gates size from the allocation, SPEC-0509)
    if lim.get("cpus") and req["cpus"] > lim["cpus"]:
        warnings.append(f"cpus {req['cpus']} clamped to {lim['cpus']} ({where} and its nodes)")
        req["cpus"] = int(lim["cpus"])
    if lim.get("mem_mb") and req["mem_mb"] > lim["mem_mb"]:
        warnings.append(f"mem {req['mem_mb']}M clamped to {lim['mem_mb']}M ({where} and its "
                        f"nodes)")
        req["mem_mb"] = int(lim["mem_mb"])
    minutes = max(1, math.ceil((req["time_s"] + sett["time_margin_s"]) / 60))
    cap = max(1, int(sett["max_time_s"] // 60))
    if lim.get("time_min"):
        cap = min(cap, int(lim["time_min"]))
    if minutes > cap:
        warnings.append(f"time {minutes} min clamped to the partition maximum {cap} min")
        minutes = cap
    payload = h.get("payload") or {}
    comment = json.dumps({"campaign": h.get("campaign"), "seq": payload.get("seq"),
                          "attempt": h.get("attempt"), "kind": h.get("kind"),
                          "requester": h.get("requester") or payload.get("requester")},
                         separators=(",", ":"))
    argv = ["--parsable", f"--job-name={h['job_id']}", f"--comment={comment}"]
    if sett.get("partition"):
        argv.append(f"--partition={sett['partition']}")
    argv += [f"--cpus-per-task={req['cpus']}", f"--mem={req['mem_mb']}M",
             f"--time={minutes}"]
    if req["licenses"]:
        known = licenses(sett)
        keep = {k: v for k, v in sorted(req["licenses"].items()) if k in known}
        for name in sorted(set(req["licenses"]) - set(keep)):
            warnings.append(f"license {name!r} is not defined in Slurm; dropped")
        if keep:
            argv.append("--licenses=" + ",".join(f"{k}:{v}" for k, v in keep.items()))
    wrap = (f"export {_attempt_env()}=$((${{SLURM_RESTART_COUNT:-0}}+1)); "
            f"exec {' '.join(shlex.quote(a) for a in nw)} worker run-job {shlex.quote(str(job))}")
    argv += [f"--chdir={job}", f"--output={job / 'slurm.out'}", "--open-mode=append",
             "--requeue", "--signal=B:TERM@60", "--export=ALL", f"--wrap={wrap}"]
    return argv, warnings


def _attempt_env() -> str:
    from . import worker_job as WJ
    return WJ.ATTEMPT_ENV


def _job_env(cfg, sett: dict) -> dict:
    """What the Slurm job inherits (`--export=ALL` of sbatch's environment): this process's, the
    worker's data/config dirs, and no detached finish gc (it would be killed with the job's
    cgroup; `start` runs one outside instead)."""
    from . import worker_job as WJ
    extra = {"NW_DATA_DIR": str(cfg["data_dir"]), W.FINISH_GC_ENV: "0"}
    if cfg.get("config_dir"):
        extra["NW_CONFIG_DIR"] = str(cfg["config_dir"])
    env = _env(sett, extra)
    env.pop(WJ.LOCK_FD_ENV, None)
    return env


_UNREACHABLE_RE = re.compile(r"unable to contact|connection refused|timed out|"
                             r"socket timed out|not responding|try again", re.I)


def _slurm_id(job: Path, rec: dict | None) -> int | None:
    for src in (rec or {}, runproc.read_json(job / "job.json") or {}):
        v = src.get("slurm_job_id")
        if isinstance(v, int) and not isinstance(v, bool):
            return v
    return None


# ---- the executor ----------------------------------------------------------------------------

class _Hold:
    def __init__(self, interrupted: bool, waited_s: float = 0.0):
        self.interrupted = interrupted
        self.waited_s = waited_s


def queued_s(cfg, job_id: str) -> float:
    """How long the job waited in Slurm's queue: from sbatch (`job.json` `spawned_ts`) to its
    process's start (`proc.json` `started_ts`); 0.0 when either is missing. It is the job's
    `slot_wait_s` (the gate record's worker wait), as a local job's host slot wait is."""
    job = W.jobs_dir(cfg) / job_id
    a = (runproc.read_json(job / "job.json") or {}).get("spawned_ts")
    b = (runproc.read_json(job / "proc.json") or {}).get("started_ts")
    ok = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (a, b))
    return round(max(0.0, b - a), 3) if ok else 0.0


class SlurmExecutor:
    name = NAME
    lease_cancels = False           # SPEC-0503/0513: a Slurm job never ends on a lapsed lease
    term_cancels = True             # `run-job` turns SIGTERM (scancel, time limit) into a cancel

    def start(self, cfg, job: Path, h: dict, lock_fd: int) -> dict:
        from . import worker_job as WJ
        sett = settings(cfg)
        argv, warnings = sbatch_argv(cfg, sett, job, h, W.own_nw())
        for w in warnings:
            print(f"nw worker: {h['job_id']}: {w}", file=sys.stderr)
        try:
            p = _run(sett, "sbatch", *argv, env=_job_env(cfg, sett))
        except SlurmUnavailable as e:
            raise W.WorkerError("spawn", f"sbatch could not run: {e}", retryable=True)
        out = (p.stdout or "").strip().split(";")[0]
        if p.returncode != 0 or not out.isdigit():
            err = (p.stderr or p.stdout or "").strip() or f"exit {p.returncode}"
            raise W.WorkerError("spawn", f"sbatch failed: {err}",
                                retryable=bool(_UNREACHABLE_RE.search(err)))
        jid = int(out)
        fields = {"slurm_job_id": jid}
        if warnings:
            fields["slurm_warnings"] = warnings
        with W.jobs_lock(cfg):
            WJ._set_job_json(job, **fields)
        W._start_gc(cfg)            # the finish gc runs here, outside any job's cgroup
        return {"slurm_job_id": jid}

    def cancel(self, cfg, job: Path, rec: dict) -> None:
        (job / "cancel").write_text(now_iso() + "\n", encoding="utf-8")
        jid = _slurm_id(job, rec)
        if jid is None:
            return
        sett = settings(cfg)
        try:
            info = lookup(sett, jid)
        except SlurmUnavailable:
            info = None
        if info is not None and info["state"] in PENDING:
            args = [jid]                       # nothing runs yet: remove it from the queue
        else:                                  # its own process ends the gates gracefully
            args = ["--batch", "--signal=TERM", jid]
        try:
            p = _run(sett, "scancel", *args)
            if p.returncode != 0:
                print(f"nw worker: scancel {jid}: {_tail(p)}", file=sys.stderr)
        except SlurmUnavailable as e:
            print(f"nw worker: scancel {jid}: {e}", file=sys.stderr)
        forget(jid)

    def state(self, cfg, job: Path, rec: dict, sett: dict, now: float) -> tuple:
        from . import worker_job as WJ
        jid = _slurm_id(job, rec)
        if jid is None:                         # spooling, or sbatch has not answered yet
            return WJ._live_state(job, sett, now)
        ss = settings(cfg)
        try:
            info = lookup(ss, jid)
        except SlurmUnavailable as e:
            print(f"nw worker: {job.name}: Slurm unavailable: {e}", file=sys.stderr)
            # never `lost` on a CLI failure (gc would reap a job Slurm still runs)
            return ("running" if (job / "proc.json").exists() else "accepted"), None
        proc = runproc.read_json(job / "proc.json")
        if info is None:
            return "lost", WJ._heartbeat_age(job, now, proc)
        if info["state"] in PENDING:
            _pending(job, jid, info["reason"] or "Pending", now)
            return "accepted", None
        if info["state"] in RUNNING:
            _backstop(ss, job, jid, now)
            return "running", WJ._heartbeat_age(job, now, proc)
        return _finish(job, jid, info), None

    @contextmanager
    def host_slot(self, cfg, h: dict, cancel, announce):
        yield _Hold(cancel.is_set(), queued_s(cfg, h["job_id"]) if h.get("job_id") else 0.0)

    def run_inline(self, cfg, argv: list, stdin, stdout) -> int:
        return gate_job(self, cfg, stdin, stdout)


def _job_frames(job: Path):
    from . import worker_job as WJ
    rec = runproc.read_json(job / "header.json") or {}
    frames = W._Frames(rec.get("job_id") or job.name, None)
    frames.closed = True
    frames.log = job / "frames.jsonl"
    frames.n = WJ._last_n(job)
    return frames


def _pending(job: Path, jid: int, reason: str, now: float) -> None:
    """Append a `slot_wait` frame `{waited_s, reason}` for a job Slurm keeps PENDING: the first
    at once, then every `NW_WORKER_SLOT_WAIT_EVENT_S` (or when the reason changes). Only while
    this reader holds `job.lock`, so no job process is writing frames meanwhile."""
    from . import worker_job as WJ
    last = runproc.read_json(job / WAIT_FILE) or {}
    every = W._slot_wait_event_s()
    if isinstance(last.get("ts"), (int, float)) and last.get("reason") == reason \
            and now - last["ts"] < every:
        return
    fd = WJ._take_lock_nb(job, 0)
    if fd is None:
        return
    try:
        if (job / "result.json").exists() or (job / "proc.json").exists():
            return
        rec = runproc.read_json(job / "job.json") or {}
        since = rec.get("spawned_ts") or rec.get("submitted_ts") or now
        _job_frames(job).emit("slot_wait", waited_s=round(max(0.0, now - since), 3),
                              reason=reason, slurm_job_id=jid)
        WJ._write_atomic(job / WAIT_FILE, {"ts": now, "reason": reason})
    except OSError as e:
        print(f"nw worker: {job.name}: could not record the Slurm wait: {e}", file=sys.stderr)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _backstop(sett: dict, job: Path, jid: int, now: float) -> None:
    """A running job whose `cancel` file is older than `cancel_grace_s` has not ended on its own
    TERM: `scancel` it (once)."""
    try:
        asked = (job / "cancel").stat().st_mtime
    except OSError:
        return
    if now - asked < sett["cancel_grace_s"] or (job / KILL_FILE).exists():
        return
    from . import worker_job as WJ
    try:
        p = _run(sett, "scancel", jid)
        WJ._write_atomic(job / KILL_FILE, {"at": now_iso(), "rc": p.returncode})
    except (SlurmUnavailable, OSError) as e:
        print(f"nw worker: {job.name}: backstop scancel failed: {e}", file=sys.stderr)
    forget(jid)


def _finish(job: Path, jid: int, info: dict) -> str:
    """A job Slurm ended without `result.json` (OOM, time limit, node failure, a cancel that
    killed it, a job process that died): write its one final frame and `result.json`, so every
    reader ends normally. A cancelled job with a `cancel` file is `done` interrupted; anything
    else is an `error` frame `{stage: "slurm", slurm_state, exit_code, infra: true}`. Under
    `job.lock` (taken without waiting: a holder is the job process still finishing, so the job
    reads `running` until it lets go)."""
    from . import worker_job as WJ
    fd = WJ._take_lock_nb(job, 0)
    if fd is None:
        return "running"
    try:
        res = runproc.read_json(job / "result.json")
        if res is not None:
            return WJ._result_state(res)
        reader = WJ._Reader(job / "frames.jsonl", 0)
        for _ in reader.read():
            pass
        rec = runproc.read_json(job / "job.json") or {}
        attempt = rec.get("run_attempts") if isinstance(rec.get("run_attempts"), int) else 1
        frames = _job_frames(job)
        extra = {}
        if reader.final is not None:            # it wrote its final frame, then died
            state = reader.final
            frames.last = None
        elif info["state"] == "CANCELLED" and (job / "cancel").exists():
            state = "done"
            frames.emit("done", ok=False, started_at=now_iso(), duration_s=0.0,
                        slot_wait_s=0.0, interrupted=True, changed=[], slurm_job_id=jid,
                        slurm_state=info["state"])
        else:
            state = "error"
            err = (f"the Slurm job {jid} ended {info['state']}"
                   + (f" ({info['reason']})" if info.get("reason") not in (None, "None") else "")
                   + " without a result")
            extra = {"stage": "slurm", "error": err}
            frames.emit("error", stage="slurm", error=err, slurm_state=info["state"],
                        slurm_job_id=jid, exit_code=info.get("exit_code"),
                        signal=info.get("signal"), infra=True)
        WJ._set_job_json(job, state=state, finished_at=now_iso(), **extra)
        WJ._write_atomic(job / "result.json", {"state": state, "frame": frames.last,
                                               "finished_at": now_iso(), "attempt": attempt,
                                               "slurm_state": info["state"]})
        return state
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ---- the sync `gate-job` -----------------------------------------------------------------------

class _Prefixed:
    """`stdin` with its first line already read: `submit` reads the header line again."""

    def __init__(self, line: bytes, stream):
        self.line, self.stream = line, stream

    def readline(self, limit: int = -1) -> bytes:
        line, self.line = self.line, b""
        return line if line else self.stream.readline(limit)

    def read(self, n: int = -1) -> bytes:
        return self.stream.read(n)

    def fileno(self):
        return self.stream.fileno()


def _watch_eof(fd: int | None, cancel: threading.Event) -> None:
    try:
        if fd is not None:
            while os.read(fd, 65536):
                pass
    except (OSError, ValueError):
        pass
    cancel.set()


def gate_job(ex: SlurmExecutor, cfg, stdin, stdout) -> int:
    """SPEC-0510's sync `gate-job`: the header and bundle are claimed and spooled exactly as
    `submit` does (same validation), the job is submitted, and its `frames.jsonl` is followed to
    stdout until `done`/`error`. stdin EOF or SIGTERM/SIGINT/SIGHUP cancels (the job reports
    `done` interrupted). Exit 0 after `done`, 3 after `error` (as the local `gate-job`)."""
    from . import worker_job as WJ
    line = stdin.readline(W.MAX_HEADER_BYTES)
    try:
        h = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        h = None
    frames = W._Frames(None, stdout)
    if isinstance(h, dict) and isinstance(h.get("job_id"), str) and W.KEY_RE.match(h["job_id"]):
        frames.job_id = h["job_id"]
        if h.get("kind") not in (None, "gate"):     # SPEC-0344: persona jobs are submit-only
            frames.emit("error", stage="validate",
                        error=f"kind {h['kind']!r} needs `nw worker submit`")
            return W.EXIT_ERROR
        if (W.jobs_dir(cfg) / h["job_id"]).exists():
            frames.emit("error", stage="claim",
                        error=f"job {h['job_id']} already exists on this worker")
            return W.EXIT_ERROR
    sink = io.BytesIO()
    rc = WJ.submit(cfg, _Prefixed(line, stdin), sink)
    got = [json.loads(ln) for ln in sink.getvalue().splitlines() if ln.strip()]
    if rc != 0 or not got or got[0].get("type") != "status":
        stdout.write(sink.getvalue())
        stdout.flush()
        return rc or W.EXIT_ERROR
    return follow(cfg, W.jobs_dir(cfg) / h["job_id"], stdin, stdout)


def follow(cfg, job: Path, stdin, stdout) -> int:
    """Copy `job`'s frames to `stdout` until its final frame; stdin EOF, a closed stdout or a
    signal cancels it (`worker_job.cancel`), then follows its interrupted ending for at most
    `FOLLOW_AFTER_CANCEL_S`."""
    from . import worker_job as WJ
    cancel = threading.Event()
    fd = W._detach_stdin(stdin)
    threading.Thread(target=_watch_eof, args=(fd, cancel), daemon=True,
                     name="slurm_watch_eof").start()
    old = W._install_stop_handler(lambda signum, _f: cancel.set())
    sett = WJ.settings(cfg)
    reader = WJ._Reader(job / "frames.jsonl", 0)
    closed, cancelled_at = False, None
    try:
        while True:
            st = WJ.job_state(cfg, job, sett)
            for ln in reader.read():
                if not closed:
                    try:
                        stdout.write(ln)
                    except (OSError, ValueError):
                        closed = True
            if not closed:
                try:
                    stdout.flush()
                except (OSError, ValueError):
                    closed = True
            if reader.final:
                return 0 if reader.final == "done" else W.EXIT_ERROR
            if closed:
                cancel.set()
            if not job.is_dir() or st["state"] == "lost":
                out = W._Frames(job.name, None if closed else stdout)
                out.emit("error", stage="lost", error=f"job {job.name} was lost on the worker")
                return W.EXIT_ERROR
            if st["state"] in ("done", "error"):
                return 0 if st["state"] == "done" else W.EXIT_ERROR
            now = time.monotonic()
            if cancel.is_set():
                if cancelled_at is None:
                    cancelled_at = now
                    WJ.cancel(cfg, job.name, io.BytesIO())
                elif now - cancelled_at > FOLLOW_AFTER_CANCEL_S:
                    return W.EXIT_ERROR
            time.sleep(WJ.FOLLOW_TICK_S)
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)


# ---- `nw worker slurm-check` -----------------------------------------------------------------

_CG_WRAP = ("nproc; p=$(cut -d: -f3 /proc/self/cgroup); m=max; "
            "while [ -n \"$p\" ] && [ \"$p\" != / ]; do "
            "v=$(cat /sys/fs/cgroup$p/memory.max 2>/dev/null); "
            "case \"$v\" in ''|max) ;; *) if [ \"$m\" = max ] || [ \"$v\" -lt \"$m\" ]; "
            "then m=$v; fi;; esac; p=$(dirname \"$p\"); done; echo \"$m\"")
_OOM_WRAP = "python3 -c 'b = bytearray(600 << 20); print(len(b))'"
END_STATES_WAIT_S = 180.0


def _wait_end(sett: dict, jid: int, bound_s: float) -> dict | None:
    end = time.monotonic() + bound_s
    while True:
        forget(jid)
        try:
            info = lookup(sett, jid)
        except SlurmUnavailable:
            info = None
        if info is not None and info["state"] not in PENDING | RUNNING:
            return info
        if time.monotonic() >= end:
            _run(sett, "scancel", jid)
            return info
        time.sleep(1.0)


def slurm_check(cfg, wait_s: float = END_STATES_WAIT_S) -> dict:
    """SPEC-0510's smoke test, as this user: the Slurm version, the node, the config, the node's
    reserved core and memory, a 2-cpu 1 GiB job that sees 2 cpus and a 1 GiB `memory.max`, and a
    200 MiB job that allocates 600 MiB and ends `OUT_OF_MEMORY`. `{schema, ok, checks: [...]}`."""
    sett = settings(cfg)
    checks = []

    def add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    def cli(name, *args):
        try:
            p = _run(sett, name, *args)
        except SlurmUnavailable as e:
            return None, str(e)
        return p, (p.stdout or "").strip() if p.returncode == 0 else _tail(p)

    p, out = cli("sinfo", "--version")
    add("version", p is not None and p.returncode == 0 and out.startswith("slurm "), out)
    p, out = cli("sinfo", "-N", "-h", "-o", "%N %c %m %T")
    add("node", p is not None and p.returncode == 0 and bool(out), out)
    p, out = cli("scontrol", "show", "config")
    keys = ("SelectType", "SelectTypeParameters", "ProctrackType", "TaskPlugin",
            "JobAcctGatherType", "AuthType", "AccountingStorageType", "PriorityType",
            "PreemptType", "MinJobAge", "KillWait")
    conf = {}
    for ln in out.splitlines() if p is not None and p.returncode == 0 else []:
        k, _, v = ln.partition("=")
        if k.strip() in keys:
            conf[k.strip()] = v.strip()
    add("config", conf.get("ProctrackType") == "proctrack/cgroup"
        and "task/cgroup" in conf.get("TaskPlugin", "")
        and conf.get("SelectType") == "select/cons_tres", conf or out)
    host = socket.gethostname().split(".")[0]
    p, out = cli("scontrol", "show", "node", host)
    spec = {k: v for k, v in re.findall(r"(CfgTRES|CoreSpecCount|MemSpecLimit)=(\S+)", out)}
    add("node_spec", p is not None and p.returncode == 0 and "CfgTRES" in spec, spec or out)
    p, out = cli("scontrol", "show", "licenses")
    add("licenses", p is not None and p.returncode == 0,
        re.findall(r"LicenseName=(\S+)", out) if p is not None else out)

    work = W.worker_dir(cfg) / "slurm-check"
    work.mkdir(parents=True, exist_ok=True)
    for name, opts, wrap, judge in (
            ("cgroup", ["--cpus-per-task=2", "--mem=1G"], _CG_WRAP,
             lambda info, txt: (info or {}).get("state") == "COMPLETED"
             and txt.split()[:2] == ["2", str(1 << 30)]),
            ("oom", ["--cpus-per-task=1", "--mem=200M"], _OOM_WRAP,
             lambda info, txt: (info or {}).get("state") == "OUT_OF_MEMORY")):
        outf = work / f"{name}.{os.getpid()}.out"
        outf.unlink(missing_ok=True)
        part = [f"--partition={sett['partition']}"] if sett.get("partition") else []
        try:
            p = _run(sett, "sbatch", "--parsable", f"--job-name=nw-slurm-check-{name}", *part,
                     *opts, "--time=2", f"--chdir={work}", f"--output={outf}", f"--wrap={wrap}")
        except SlurmUnavailable as e:
            add(name, False, str(e))
            continue
        jid = (p.stdout or "").strip().split(";")[0]
        if p.returncode != 0 or not jid.isdigit():
            add(name, False, f"sbatch: {_tail(p)}")
            continue
        info = _wait_end(sett, int(jid), wait_s)
        try:
            txt = outf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            txt = ""
        add(name, judge(info, txt), {"job_id": int(jid), "state": (info or {}).get("state"),
                                     "output": txt.strip()[-400:]})
        outf.unlink(missing_ok=True)
    return {"schema": CHECK_SCHEMA, "ok": all(c["ok"] for c in checks), "checks": checks}


def remote_check(w: dict) -> tuple[list[str], dict | None]:
    """The coordinator's `nw workers check NAME --slurm`: `nw worker slurm-check --json` on the
    worker over SSH. `(errors, the check record)`; no errors means every check passed."""
    from . import remote_gates as RG
    got = RG._call(w, "slurm-check", "--json", timeout=2 * END_STATES_WAIT_S + 120)
    if got is None:
        return ["`nw worker slurm-check` could not run over SSH"], None
    rc, lines, err = got
    rec = next((r for r in lines if r.get("schema") == CHECK_SCHEMA), None)
    if rec is None:
        tail = (err or b"").decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
        return [f"`nw worker slurm-check` failed (exit {rc}): {tail[0]}".rstrip(": ")], None
    bad = [f"slurm {c.get('name')}: {c.get('detail')}" for c in rec.get("checks") or []
           if isinstance(c, dict) and not c.get("ok")]
    return bad, rec
