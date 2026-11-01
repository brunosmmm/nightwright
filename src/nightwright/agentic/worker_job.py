"""Worker-owned async gate jobs (SPEC-0340): `submit`, `attach`, `poll`, `cancel`, `jobs`, and
the detached job process (`python -m nightwright.agentic.worker_job <job_dir>`).

`submit` claims the job dir (as `gate-job` does), spools the header and bundle into it, writes
the first `lease.json` and starts the job process in a session of its own, handing it the
`job.lock` flock (an inherited fd), so the dir is never unlocked between the two. The job
process claims `proc.json`, heartbeats, and runs the same checkout and gate steps as
`gate-job`, appending every frame to `frames.jsonl` with a sequence number `n` (from 1). It
cancels itself on a `cancel` file or an expired lease; `result.json` is its last word.

A client never owns the job: `attach` follows `frames.jsonl` from a cursor and renews the lease
while it is connected, `poll` reads once. A hung or dead job process is `lost`; `cancel` and
`gc` reap it from outside (TERM, then KILL, each start-ticks-checked)."""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import gates as G
from . import runproc
from . import worker as W
from .store import now_iso

DEFAULTS = {"job_lease_s": 900.0, "job_heartbeat_s": 10.0, "job_lost_after_s": 60.0}
STATES = ("accepted", "running", "done", "error", "lost")
FOLLOW_TICK_S = 0.5             # attach's follow tick
WATCH_TICK_S = 0.25             # the job process's cancel / lease / gate-group checks
LEASE_REWRITE_MAX_S = 30.0
# the TERM -> KILL grace of the lost-job kill path (tests shorten it)
KILL_GRACE_ENV = "NW_WORKER_KILL_GRACE_S"
DEFAULT_KILL_GRACE_S = 5.0
# the fd number of the `job.lock` flock submit hands its job process
LOCK_FD_ENV = "NW_WORKER_JOB_LOCK_FD"
EXIT_UNKNOWN = 1
EXIT_LOST = 4


def settings(cfg) -> dict:
    """`agentic.workers.{job_lease_s, job_heartbeat_s, job_lost_after_s}` over `DEFAULTS`; a
    value that is not a positive finite number falls back to its default."""
    raw = (cfg.get("agentic") or {}).get("workers")
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for key, default in DEFAULTS.items():
        v = raw.get(key, default)
        try:
            f = float(v)
        except (TypeError, ValueError, OverflowError):
            f = None
        ok = f is not None and not isinstance(v, bool) and math.isfinite(f) and f > 0
        out[key] = f if ok else default
    return out


def header_digest(h: dict) -> str:
    """sha256 of the whole header as received, in its sorted compact JSON form."""
    text = json.dumps(h, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_atomic(path: Path, rec: dict) -> None:
    """Write `rec` to `path` via a temp file private to this process and thread (attach, poll
    and the job process may rewrite the same file at once)."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _kill_grace_s() -> float:
    return W._float_env(KILL_GRACE_ENV, DEFAULT_KILL_GRACE_S)


def _status(job_id, state, **extra) -> dict:
    return {"schema": W.FRAME_SCHEMA, "type": "status", "job_id": job_id, "at": now_iso(),
            "state": state, **extra}


def _print(stdout, rec: dict) -> None:
    stdout.write((json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))
    stdout.flush()


# ---- job state -------------------------------------------------------------------------------

def _is_async(job: Path) -> bool:
    """A `submit` job dir (it spools `header.json`); a SPEC-0336 `gate-job` dir has none."""
    return (job / "header.json").exists()


def _lock_held(job: Path) -> bool:
    live, fd = W._job_live(job)
    if fd is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return live


def _pid_alive(proc: dict) -> bool:
    if not proc.get("pid"):
        return False
    return runproc.wrapper_alive({"wrapper_pid": proc["pid"],
                                  "wrapper_start_ticks": proc.get("start_ticks")})


def _heartbeat_age(job: Path, now: float, proc: dict | None) -> float | None:
    hb = runproc.read_json(job / "heartbeat.json") or {}
    ts = hb.get("ts")
    if not isinstance(ts, (int, float)) and proc:
        ts = proc.get("started_ts")
    return round(max(0.0, now - ts), 3) if isinstance(ts, (int, float)) else None


def _result_state(res: dict) -> str:
    st = res.get("state")
    return st if st in ("done", "error", "lost") else "error"


def _live_state(job: Path, sett: dict, now: float) -> tuple[str, float | None]:
    """The state of a job without `result.json`."""
    lost_after = sett["job_lost_after_s"]
    proc = runproc.read_json(job / "proc.json")
    age = _heartbeat_age(job, now, proc)
    if proc is None:
        rec = runproc.read_json(job / "job.json") or {}
        if not _is_async(job):                  # a gate-job: its stream owns it
            if rec.get("state") in ("done", "error"):
                return rec["state"], None
            return ("running" if _lock_held(job) else "lost"), None
        since = rec.get("spawned_ts")
        if not isinstance(since, (int, float)):
            if _lock_held(job):                 # submit is still spooling the bundle
                return "accepted", None
            since = rec.get("submitted_ts")
        if isinstance(since, (int, float)) and now - since <= lost_after:
            return "accepted", None
        return "lost", None
    if not _pid_alive(proc):
        return "lost", age
    if age is not None and age > lost_after:     # alive but no heartbeat: hung
        return "lost", age
    return "running", age


def job_state(cfg, job: Path, sett: dict | None = None) -> dict:
    """`{state, heartbeat_age_s}` of a job dir, derived the same way for every reader."""
    sett = sett or settings(cfg)
    now = time.time()
    res = runproc.read_json(job / "result.json")
    if res is None:
        state, age = _live_state(job, sett, now)
        if state != "lost":
            return {"state": state, "heartbeat_age_s": age}
        res = runproc.read_json(job / "result.json")    # it may have ended between the reads
        if res is None:
            return {"state": "lost", "heartbeat_age_s": age}
    proc = runproc.read_json(job / "proc.json")
    return {"state": _result_state(res), "heartbeat_age_s": _heartbeat_age(job, now, proc)}


def _set_job_json(job: Path, **fields) -> None:
    rec = runproc.read_json(job / "job.json") or {}
    rec.update(fields)
    _write_atomic(job / "job.json", rec)


# ---- frames ----------------------------------------------------------------------------------

class _Reader:
    """Complete lines of `frames.jsonl` whose `n` is above the cursor. A trailing line without
    its newline (a frame still being written) is held back until the newline appears."""

    def __init__(self, path: Path, after: int):
        self.path = path
        self.after = after
        self.pos = 0
        self.final: str | None = None           # `done` / `error` once emitted

    def read(self) -> list[bytes]:
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read()
        except FileNotFoundError:
            return []
        end = data.rfind(b"\n")
        if end < 0:
            return []
        self.pos += end + 1
        out = []
        for raw in data[:end].split(b"\n"):
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            n = rec.get("n") if isinstance(rec, dict) else None
            if not isinstance(n, int) or isinstance(n, bool) or n <= self.after:
                continue
            self.after = n
            out.append(raw + b"\n")
            if rec.get("type") in ("done", "error"):
                self.final = rec["type"]
        return out


def _last_n(job: Path) -> int:
    r = _Reader(job / "frames.jsonl", 0)
    r.read()
    return r.after


def _renew(job: Path, lease_s: float) -> None:
    _write_atomic(job / "lease.json", {"lease_until": time.time() + lease_s,
                                       "renewed_at": now_iso()})


# ---- the lost-job kill path ------------------------------------------------------------------

def _kill_group(child: dict, grace: float, cont: bool = False) -> None:
    """TERM the group (and CONT it, so a stopped process can take the TERM), wait up to
    `grace`, then KILL. `runproc.group_alive` checks the leader's start ticks first, so a
    recycled pid is never signalled."""
    if not child.get("pgid") or not runproc.group_alive(child):
        return
    pgid = int(child["pgid"])
    for sig, wait_s in ((signal.SIGTERM, grace), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(pgid, sig)
            if cont and sig == signal.SIGTERM:
                os.killpg(pgid, signal.SIGCONT)
        except OSError:
            return
        end = time.monotonic() + wait_s
        while time.monotonic() < end:
            if not runproc.group_alive(child):
                return
            time.sleep(0.05)


def reap(job: Path) -> None:
    """End a lost job's job process group and every gate group `proc.json` records, then
    write `result.json` with `state: "lost"` (unless the job wrote its own meanwhile)."""
    proc = runproc.read_json(job / "proc.json") or {}
    grace = _kill_grace_s()
    if proc.get("pid") and _pid_alive(proc):
        _kill_group({"pid": proc["pid"], "pgid": proc.get("pgid") or proc["pid"],
                     "start_ticks": proc.get("start_ticks")}, grace, cont=True)
    for gate in proc.get("gates") or []:
        if isinstance(gate, dict):
            _kill_group(gate, grace)
    if not (job / "result.json").exists():
        _write_atomic(job / "result.json", {"state": "lost", "frame": None,
                                            "reaped_at": now_iso()})
        _set_job_json(job, state="lost", finished_at=now_iso())


def reap_lost(cfg) -> list[str]:
    """`gc`'s first step: reap every async job that is `lost` with no `result.json`. Caller
    holds `jobs_lock`."""
    sett = settings(cfg)
    out = []
    jd = W.jobs_dir(cfg)
    for job in sorted(jd.iterdir()) if jd.is_dir() else []:
        if not job.is_dir() or not _is_async(job) or (job / "result.json").exists():
            continue
        if job_state(cfg, job, sett)["state"] == "lost":
            reap(job)
            out.append(job.name)
    return out


# ---- submit ----------------------------------------------------------------------------------

def _end_with_error(cfg, h, job, frames, job_rec, stage, error) -> int:
    """A claimed job that never got a job process: `error` frame, `result.json`, cleanup."""
    frames.emit("error", stage=stage, error=error)
    try:
        job_rec.update(state="error", stage=stage, error=error, finished_at=now_iso())
        _write_atomic(job / "job.json", job_rec)
        W._cleanup(cfg, h, job, start_gc=False)
        _write_atomic(job / "result.json", {"state": "error", "frame": frames.last,
                                            "finished_at": now_iso()})
    except Exception as e:
        print(f"nw worker: cleanup after error failed: {e}", file=sys.stderr)
    return W.EXIT_ERROR


def _spawn(cfg, job: Path, lock_fd: int) -> None:
    env = {**os.environ, "NW_DATA_DIR": str(cfg["data_dir"]), LOCK_FD_ENV: str(lock_fd)}
    if cfg.get("config_dir"):
        env["NW_CONFIG_DIR"] = str(cfg["config_dir"])
    with open(job / "stderr.log", "ab") as err:
        subprocess.Popen([sys.executable, "-m", "nightwright.agentic.worker_job", str(job)],
                         env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=err, start_new_session=True, pass_fds=(lock_fd,))


def submit(cfg, stdin, stdout) -> int:
    """`nw worker submit`: claim, spool, start the job process, print `accepted` and exit 0.
    A resubmit with the same header digest prints the current state; another header is an
    `error` (`stage: "claim"`, exit 3)."""
    frames = W._Frames(None, stdout)
    try:
        line = stdin.readline(W.MAX_HEADER_BYTES)
        try:
            h = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise W.WorkerError("validate", f"the header is not JSON: {e}")
        if isinstance(h, dict) and isinstance(h.get("job_id"), str) \
                and W.KEY_RE.match(h["job_id"]):
            frames.job_id = h["job_id"]
        W.validate_header(h)
    except W.WorkerError as e:
        frames.emit("error", stage=e.stage, error=e.error)
        return W.EXIT_ERROR
    digest = header_digest(h)
    sett = settings(cfg)
    job = W.jobs_dir(cfg) / h["job_id"]
    with W.jobs_lock(cfg):
        W.jobs_dir(cfg).mkdir(parents=True, exist_ok=True)
        try:
            job.mkdir()
        except FileExistsError:
            rec = runproc.read_json(job / "job.json") or {}
            if rec.get("header_digest") == digest:          # idempotent: nothing new starts
                st = job_state(cfg, job, sett)
                _print(stdout, _status(h["job_id"], st["state"], n=_last_n(job),
                                       heartbeat_age_s=st["heartbeat_age_s"]))
                return 0
            why = "with a different header" if rec.get("header_digest") else "(a gate-job)"
            frames.emit("error", stage="claim",
                        error=f"job {h['job_id']} already exists on this worker {why}")
            return W.EXIT_ERROR
        lock_fd = os.open(job / "job.lock", os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        frames.log = job / "frames.jsonl"
        payload = h["payload"]
        job_rec = {"schema": "nw.worker.job.v1", "job_id": h["job_id"], "kind": h["kind"],
                   "campaign": h["campaign"], "spec_id": h["spec_id"],
                   "repo_key": h["repo_key"], "commit": h["commit"], "base": h["base"],
                   "attempt": h.get("attempt"), "seq": payload["seq"],
                   "tier": payload.get("tier"), "pid": os.getpid(), "claimed_at": now_iso(),
                   "state": "accepted", "header_digest": digest, "submitted_ts": time.time()}
        _write_atomic(job / "job.json", job_rec)
        _write_atomic(job / "header.json", h)
    try:
        try:
            if h["bundle_bytes"]:
                W._read_exact(stdin, h["bundle_bytes"], job / "bundle")
            _renew(job, sett["job_lease_s"])     # the first lease, before the process exists
            job_rec["spawned_ts"] = time.time()
            _write_atomic(job / "job.json", job_rec)
            _spawn(cfg, job, lock_fd)
        except W.WorkerError as e:
            return _end_with_error(cfg, h, job, frames, job_rec, e.stage, e.error)
        except OSError as e:
            return _end_with_error(cfg, h, job, frames, job_rec, "spawn",
                                   f"could not start the job process: {e}")
    finally:
        os.close(lock_fd)                       # the job process keeps the flock
    _print(stdout, _status(h["job_id"], "accepted", n=0))
    return 0


# ---- attach / poll / cancel / jobs -----------------------------------------------------------

def _job_or_none(cfg, job_id) -> Path | None:
    if not isinstance(job_id, str) or not W.KEY_RE.match(job_id):
        return None
    job = W.jobs_dir(cfg) / job_id
    return job if job.is_dir() else None


def _unknown(stdout, job_id) -> int:
    _print(stdout, _status(job_id, "unknown"))
    return EXIT_UNKNOWN


def attach(cfg, job_id, after: int, stdout) -> int:
    """`nw worker attach JOB --from N`: the frames after `N`, then follow every 0.5 s. Exit 0
    after `done`, 3 after `error`, 4 with a `lost` status line. The lease is renewed on every
    tick (rewritten once `min(30, job_lease_s/3)` s passed). EOF or a signal never cancels."""
    job = _job_or_none(cfg, job_id)
    if job is None:
        return _unknown(stdout, job_id)
    sett = settings(cfg)
    lease_s = sett["job_lease_s"]
    rewrite_every = min(LEASE_REWRITE_MAX_S, lease_s / 3)
    reader = _Reader(job / "frames.jsonl", after)
    renewed = None
    while True:
        st = job_state(cfg, job, sett)          # before reading: a final frame is never missed
        for line in reader.read():
            stdout.write(line)
        stdout.flush()
        if reader.final:
            return 0 if reader.final == "done" else W.EXIT_ERROR
        if st["state"] == "lost":
            _print(stdout, _status(job_id, "lost", heartbeat_age_s=st["heartbeat_age_s"]))
            return EXIT_LOST
        if st["state"] in ("done", "error"):    # the cursor was already past the final frame
            _print(stdout, _status(job_id, st["state"], n=reader.after))
            return 0 if st["state"] == "done" else W.EXIT_ERROR
        now = time.monotonic()
        if renewed is None or now - renewed > rewrite_every:
            _renew(job, lease_s)
            renewed = now
        time.sleep(FOLLOW_TICK_S)


def poll(cfg, job_id, after: int, renew: bool, stdout) -> int:
    """`nw worker poll JOB --from N [--renew]`: the complete frames after `N` once, then a
    `status` line. `--renew` sets `lease_until = now + job_lease_s`."""
    job = _job_or_none(cfg, job_id)
    if job is None:
        return _unknown(stdout, job_id)
    sett = settings(cfg)
    if renew:
        _renew(job, sett["job_lease_s"])
    st = job_state(cfg, job, sett)
    reader = _Reader(job / "frames.jsonl", after)
    for line in reader.read():
        stdout.write(line)
    _print(stdout, _status(job_id, st["state"], heartbeat_age_s=st["heartbeat_age_s"],
                           n=reader.after))
    return 0


def cancel(cfg, job_id, stdout) -> int:
    """`nw worker cancel JOB`: `cancel` file for an accepted/running job, a no-op for a finished
    one, the kill path for a lost one."""
    job = _job_or_none(cfg, job_id)
    if job is None:
        return _unknown(stdout, job_id)
    st = job_state(cfg, job)["state"]
    if st in ("accepted", "running"):
        if not _is_async(job):
            print("nw worker: a gate-job is cancelled by closing its stream", file=sys.stderr)
            _print(stdout, _status(job_id, st))
            return EXIT_UNKNOWN
        (job / "cancel").write_text(now_iso() + "\n", encoding="utf-8")
        _print(stdout, _status(job_id, st, cancel="requested"))
        return 0
    if st == "lost":
        reap(job)
    _print(stdout, _status(job_id, st))
    return 0


def jobs(cfg) -> list[dict]:
    """Every job dir with its derived state."""
    sett = settings(cfg)
    out = []
    jd = W.jobs_dir(cfg)
    for job in sorted(jd.iterdir()) if jd.is_dir() else []:
        if not job.is_dir():
            continue
        rec = runproc.read_json(job / "job.json") or {}
        st = job_state(cfg, job, sett)
        out.append({"job_id": job.name, "state": st["state"],
                    "heartbeat_age_s": st["heartbeat_age_s"], "async": _is_async(job),
                    "campaign": rec.get("campaign"), "spec_id": rec.get("spec_id"),
                    "claimed_at": rec.get("claimed_at"), "n": _last_n(job)})
    return out


# ---- the job process -------------------------------------------------------------------------

def _take_lock(job: Path) -> int | None:
    """The `job.lock` flock submit handed over (`LOCK_FD_ENV`), else take it ourselves."""
    lock = job / "job.lock"
    raw = os.environ.get(LOCK_FD_ENV)
    if raw and raw.isdigit():
        try:
            a, b = os.fstat(int(raw)), os.stat(lock)
            if (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino):
                return int(raw)
        except OSError:
            pass
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _heartbeat(job: Path, every: float, stop: threading.Event) -> None:
    while True:
        try:
            _write_atomic(job / "heartbeat.json", {"ts": time.time(), "pid": os.getpid()})
        except OSError:
            pass
        if stop.wait(every):
            return


def _watch(job: Path, proc: dict, cancel: threading.Event, stop: threading.Event) -> None:
    """Every `WATCH_TICK_S`: record new gate process groups in `proc.json`; cancel the job on
    a `cancel` file or an expired lease."""
    recorded: dict[int, dict] = {}
    while not stop.wait(WATCH_TICK_S):
        with G._LOCK:
            live = [p.pid for p in G._LIVE]
        new = [pid for pid in live if pid not in recorded]
        if new:
            for pid in new:                     # a gate runs in a session of its own
                recorded[pid] = {"pid": pid, "pgid": pid, "start_ticks": runproc.start_ticks(pid)}
            proc["gates"] = list(recorded.values())
            try:
                _write_atomic(job / "proc.json", proc)
            except OSError:
                pass
        if cancel.is_set():
            continue
        why = None
        if (job / "cancel").exists():
            why = "cancel requested"
        else:
            until = (runproc.read_json(job / "lease.json") or {}).get("lease_until")
            if isinstance(until, (int, float)) and time.time() > until:
                why = "the lease expired"
        if why:
            print(f"nw worker job: {why}; cancelling", file=sys.stderr)
            cancel.set()
            G.terminate_all()


def run_job(cfg, job: Path, proc: dict) -> int:
    """The job's checkout and gates, frames into `frames.jsonl`, then `result.json`."""
    sett = settings(cfg)
    h = json.loads((job / "header.json").read_text(encoding="utf-8"))
    cancel, stop = threading.Event(), threading.Event()
    frames = W._Frames(h["job_id"], None, cancel)
    frames.closed = True                        # no stream: the file is the only copy
    frames.log = job / "frames.jsonl"
    frames.n = _last_n(job)
    _set_job_json(job, state="running", pid=os.getpid(), started_at=now_iso())
    threading.Thread(target=_heartbeat, args=(job, sett["job_heartbeat_s"], stop),
                     daemon=True).start()
    threading.Thread(target=_watch, args=(job, proc, cancel, stop), daemon=True).start()
    stage, state, extra = "checkout", "error", {}
    try:
        W._checkout(cfg, h, job, None, bundle=job / "bundle")
        stage = "slot"
        frames.emit("accepted")
        if cancel.is_set():                     # cancelled (or the lease ran out) meanwhile
            frames.emit("done", ok=False, started_at=now_iso(), duration_s=0.0,
                        slot_wait_s=0.0, interrupted=True)
        else:
            W._run_gates(cfg, h, job, frames, cancel)
        state = "done"
    except W.WorkerError as e:
        extra = {"stage": e.stage, "error": e.error}
    except Exception as e:                      # any exception becomes an error frame
        extra = {"stage": stage, "error": f"{type(e).__name__}: {e}"}
    if extra:
        if "done" in frames.sent:               # `done` is out: never a second final frame
            print(f"nw worker job: after done: {extra['error']}", file=sys.stderr)
            state, extra = "done", {}
        else:
            frames.emit("error", **extra)
    try:
        _set_job_json(job, state=state, finished_at=now_iso(), **extra)
        W._cleanup(cfg, h, job, start_gc=False)
    except Exception as e:
        print(f"nw worker job: cleanup failed: {e}", file=sys.stderr)
    _write_atomic(job / "result.json", {"state": state, "frame": frames.last,
                                        "finished_at": now_iso()})
    stop.set()
    W._start_gc(cfg)
    return 0 if state == "done" else W.EXIT_ERROR


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    job = Path(argv[0])
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    from ..config import load_config
    cfg = load_config()
    lock_fd = _take_lock(job)
    if lock_fd is None:
        print(f"nw worker job: {job} is locked by another process", file=sys.stderr)
        return W.EXIT_ERROR
    if (job / "result.json").exists():
        print(f"nw worker job: {job} already has a result", file=sys.stderr)
        return W.EXIT_ERROR
    me = os.getpid()
    proc = {"pid": me, "pgid": os.getpgid(0), "start_ticks": runproc.start_ticks(me),
            "started_ts": time.time(), "started_at": now_iso(), "gates": []}
    if not runproc.claim(job, proc):
        print(f"nw worker job: {job} is already claimed", file=sys.stderr)
        return W.EXIT_ERROR
    return run_job(cfg, job, proc)


if __name__ == "__main__":
    sys.exit(main())
