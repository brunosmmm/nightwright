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

import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
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
    value that is not a positive finite number falls back to its default. The heartbeat is
    capped at a third of `job_lost_after_s`: a slower one would make a healthy job look hung,
    and the kill path (gc after any job) would then reap it."""
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
    out["job_heartbeat_s"] = min(out["job_heartbeat_s"], out["job_lost_after_s"] / 3)
    return out


def header_digest(h: dict) -> str:
    """sha256 of the whole header as received, in its sorted compact JSON form."""
    text = json.dumps(h, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_atomic(path: Path, rec: dict) -> None:
    """Write `rec` to `path` via a temp file private to this process and thread (attach, poll
    and the job process may rewrite the same file at once)."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:                       # a full disk: never leave the temp file
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _kill_grace_s() -> float:
    return W._float_env(KILL_GRACE_ENV, DEFAULT_KILL_GRACE_S)


def _status(job_id, state, **extra) -> dict:
    return {"schema": W.FRAME_SCHEMA, "type": "status", "job_id": job_id, "at": now_iso(),
            "state": state, **extra}


def _print(stdout, rec: dict) -> None:
    stdout.write((json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))
    stdout.flush()


# ---- job state -------------------------------------------------------------------------------

def _is_async(job: Path, rec: dict | None = None) -> bool:
    """A `submit` job dir (`header.json`, or a `job.json` with its `header_digest` just before
    it); a SPEC-0336 `gate-job` dir has neither."""
    if (job / "header.json").exists():
        return True
    rec = runproc.read_json(job / "job.json") if rec is None else rec
    return bool((rec or {}).get("header_digest"))


def _lock_held(job: Path) -> bool | None:
    """Whether another process holds `job.lock`; None when there is no `job.lock` yet (a claim
    in progress). A reader never creates the file (it would resurrect a dir gc is removing)."""
    try:
        fd = os.open(job / "job.lock", os.O_RDWR)
    except FileNotFoundError:
        return None
    except OSError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    return False


def _pid_alive(proc: dict) -> bool:
    if not proc.get("pid"):
        return False
    return runproc.wrapper_alive({"wrapper_pid": proc["pid"],
                                  "wrapper_start_ticks": proc.get("start_ticks")})


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _heartbeat_age(job: Path, now: float, proc: dict | None) -> float | None:
    """Seconds since the job process's last heartbeat (else its start). Measured on
    `CLOCK_MONOTONIC` (`mono`, shared by every process on the host) when the record has it:
    that clock neither jumps with an NTP step nor counts a host suspend, so neither turns a
    healthy job `lost` (and gc would then reap it). The wall clock (`ts`) is the fallback."""
    hb = runproc.read_json(job / "heartbeat.json") or {}
    src = hb if _num(hb.get("ts")) else (proc or {})
    key = "ts" if src is hb else "started_ts"
    mono = src.get("mono") if src is hb else src.get("started_mono")
    if _num(mono) and src.get("boot") is not None and src.get("boot") == _boot():
        return round(max(0.0, time.monotonic() - mono), 3)
    ts = src.get(key)
    return round(max(0.0, now - ts), 3) if _num(ts) else None


_BOOT: list = []


def _boot() -> str | None:
    """This boot's id (`CLOCK_MONOTONIC` restarts with each boot): a `mono` recorded under
    another boot is never compared."""
    if not _BOOT:
        try:
            _BOOT.append(Path("/proc/sys/kernel/random/boot_id").read_text().strip() or None)
        except OSError:
            _BOOT.append(None)
    return _BOOT[0]


def _result_state(res: dict) -> str:
    st = res.get("state")
    return st if st in ("done", "error", "lost") else "error"


def _live_state(job: Path, sett: dict, now: float) -> tuple[str, float | None]:
    """The state of a job without `result.json`."""
    lost_after = sett["job_lost_after_s"]
    proc = runproc.read_json(job / "proc.json")
    age = _heartbeat_age(job, now, proc)
    if proc is None:
        rec = runproc.read_json(job / "job.json")
        held = _lock_held(job)
        if rec is None or held is None:         # mid-claim (under jobs_lock): not yet anything
            return "accepted", None
        if not _is_async(job, rec):             # a gate-job: its stream owns it
            if rec.get("state") in ("done", "error"):
                return rec["state"], None
            return ("running" if held else "lost"), None
        since = rec.get("spawned_ts")
        if not isinstance(since, (int, float)):
            if held:                            # submit is still spooling the bundle
                return "accepted", None
            since = rec.get("submitted_ts")
        if isinstance(since, (int, float)) and now - since <= lost_after:
            return "accepted", None
        return "lost", None
    if not _pid_alive(proc):
        own = (runproc.read_json(job / "job.json") or {}).get("state")
        if own in ("done", "error") and not runproc.group_alive(_leader(proc)):
            # it died after its final job.json (in its cleanup), with nothing of it left
            # running: its own final state stands (and `frames.jsonl` ends in that frame)
            return own, age
        return "lost", age
    if age is not None and age > lost_after:     # alive but no heartbeat: hung
        return "lost", age
    return "running", age


def _leader(rec: dict) -> dict:
    """A job process's own group (`proc.json` / `spawn.json`), as the kill path signals it."""
    return {"pid": rec.get("pid"), "pgid": rec.get("pgid") or rec.get("pid"),
            "start_ticks": rec.get("start_ticks")}


def job_state(cfg, job: Path, sett: dict | None = None, locked: bool = False) -> dict:
    """`{state, heartbeat_age_s}` of a job dir, derived the same way for every reader. A final
    state (`done`, `error`, `lost`) is mirrored into an async job's `job.json` `state`
    (`locked`: the caller holds `jobs_lock`; otherwise the mirror is best effort)."""
    sett = sett or settings(cfg)
    now = time.time()
    res = runproc.read_json(job / "result.json")
    if res is None:
        state, age = _live_state(job, sett, now)
        if state != "lost":
            return {"state": state, "heartbeat_age_s": age}
        res = runproc.read_json(job / "result.json")    # it may have ended between the reads
        if res is None:
            _mirror(cfg, job, "lost", locked)
            return {"state": "lost", "heartbeat_age_s": age}
    proc = runproc.read_json(job / "proc.json")
    state = _result_state(res)
    _mirror(cfg, job, state, locked)
    return {"state": state, "heartbeat_age_s": _heartbeat_age(job, now, proc)}


def _set_job_json(job: Path, **fields) -> None:
    rec = runproc.read_json(job / "job.json") or {}
    rec.update(fields)
    _write_atomic(job / "job.json", rec)


def _mirror_locked(job: Path, state: str) -> None:
    """`_mirror`'s write, under `jobs_lock`. Both files are read again here: `result.json`, if
    it is there now, wins; and a `done`/`error` the job process wrote itself (under
    `jobs_lock`, just before its `result.json`) is never overwritten by a derived `lost`."""
    rec = runproc.read_json(job / "job.json")
    if rec is None:
        return
    res = runproc.read_json(job / "result.json")
    if res is not None:
        state = _result_state(res)
    elif rec.get("state") in ("done", "error"):
        return
    if rec.get("state") != state:
        _set_job_json(job, state=state)


def _mirror(cfg, job: Path, state: str, locked: bool) -> None:
    """Write a derived final `state` into an async job's `job.json` when it says otherwise.
    Only under `jobs_lock` (taken here without waiting, else skipped: the next reader does
    it), so a write never lands in a dir gc is removing."""
    rec = runproc.read_json(job / "job.json")
    if rec is None or rec.get("state") == state or not _is_async(job, rec):
        return
    if locked:
        with contextlib.suppress(OSError):      # a mirror is best effort (a full disk)
            _mirror_locked(job, state)
        return
    try:
        fd = os.open(W.worker_dir(cfg) / "jobs.lock", os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return
    try:
        if job.is_dir():
            _mirror_locked(job, state)
    except OSError:
        pass
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ---- frames ----------------------------------------------------------------------------------

class _Reader:
    """Complete lines of `frames.jsonl` whose `n` is above the cursor. A trailing line without
    its newline (a frame still being written) is held back until the newline appears.

    The file is read in `CHUNK` pieces from the end of the last complete line read: `pos` never
    moves past an unterminated tail, which is read again next time. The job process cuts a
    failed append back off and writes the next frame at the same offset (`_Frames._append`), so
    a tail read before the cut may no longer be in the file; a complete line never is cut. A
    line at or below the cursor is skipped by the `n` at its end (a frame's last key) and never
    JSON-parsed, and only one line is held at a time: a high cursor costs one pass over the
    file, not every earlier frame in memory at once."""

    CHUNK = 1 << 20

    def __init__(self, path: Path, after: int):
        self.path = path
        self.after = after
        self.pos = 0                            # just past the last complete line read
        self.final: str | None = None           # `done` / `error` once emitted

    def read(self):
        """Yield the newly complete lines after the cursor (each ending in a newline)."""
        try:
            f = open(self.path, "rb")
        except FileNotFoundError:
            return
        with f:
            f.seek(self.pos)
            buf = b""                           # this read's line so far (across chunks)
            while True:
                data = f.read(self.CHUNK)
                if not data:
                    return                      # an unterminated `buf` is dropped: re-read
                start = 0
                while True:
                    nl = data.find(b"\n", start)
                    if nl < 0:
                        break
                    line, buf = buf + data[start:nl], b""
                    self.pos += len(line) + 1
                    out = self._take(line)
                    if out is not None:
                        yield out
                    start = nl + 1
                buf += data[start:]

    def _take(self, raw: bytes) -> bytes | None:
        m = _TAIL_N_RE.search(raw[-64:])
        if m is not None and int(m.group(1)) <= self.after:
            return None                         # at or below the cursor: never parsed
        try:
            rec = json.loads(raw)
        except ValueError:
            return None
        n = rec.get("n") if isinstance(rec, dict) else None
        if not isinstance(n, int) or isinstance(n, bool) or n <= self.after:
            return None
        self.after = n
        if rec.get("type") in ("done", "error"):
            self.final = rec["type"]
        return raw + b"\n"


_TAIL_N_RE = re.compile(rb'"n": ?(\d+)\}\s*\Z')


def _last_n(job: Path) -> int:
    """The `n` of the last complete line of `frames.jsonl`, read from the end: the frames
    before it (gate frames can be MiBs each) are never read. `n` is a frame's last key, so the
    line's last bytes usually carry it; otherwise that one line is parsed."""
    try:
        f = open(job / "frames.jsonl", "rb")
    except OSError:
        return 0
    with f:
        size = f.seek(0, os.SEEK_END)
        end = pos = size                        # `end`: just past the last complete line
        while pos > 0:
            step = min(1 << 16, pos)
            pos -= step
            f.seek(pos)
            nl = f.read(step).rfind(b"\n")
            if nl >= 0:
                end = pos + nl + 1
                break
        else:
            return 0                            # no complete line yet
        f.seek(max(0, end - 64))
        m = _TAIL_N_RE.search(f.read(end - max(0, end - 64)))
        if m:
            return int(m.group(1))
        start = pos = end - 1
        while pos > 0:                          # back to the line's start
            step = min(1 << 16, pos)
            pos -= step
            f.seek(pos)
            nl = f.read(step).rfind(b"\n")
            if nl >= 0:
                start = pos + nl + 1
                break
        else:
            start = 0
        f.seek(start)
        try:
            n = json.loads(f.read(end - start)).get("n")
        except (ValueError, AttributeError):
            return 0
        return n if isinstance(n, int) and not isinstance(n, bool) else 0


def _renew(job: Path, lease_s: float) -> None:
    _write_atomic(job / "lease.json", {"lease_until": time.time() + lease_s,
                                       "renewed_at": now_iso()})


def _renew_live(cfg, job: Path, lease_s: float) -> bool:
    """A client's lease renewal (`attach`, `poll --renew`): under `jobs_lock`, and only while
    the dir still exists, so a renewal never lands in a dir gc is removing (its rmtree would
    then leave a stub holding only `lease.json`, read as `accepted`). False when it is gone."""
    with W.jobs_lock(cfg):
        if not job.is_dir():
            return False
        try:
            _renew(job, lease_s)
        except OSError:
            if not job.is_dir():
                return False
            raise
    return True


# ---- the lost-job kill path ------------------------------------------------------------------

def _kill_group(child: dict, grace: float, cont: bool = False) -> None:
    """TERM the group (and CONT it, so a stopped process can take the TERM), wait up to
    `grace`, then KILL. `runproc.group_alive` checks the leader's start ticks first, so a
    recycled pid is never signalled; an entry without start ticks is never signalled at all."""
    if not child.get("pgid") or child.get("start_ticks") is None \
            or not runproc.group_alive(child):
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


def _take_lock_nb(job: Path, wait_s: float) -> int | None:
    """`job.lock`'s flock within `wait_s` (an fd), else None. Never creates the file: a job
    that has none is a claim in progress, never lost."""
    try:
        fd = os.open(job / "job.lock", os.O_RDWR)
    except OSError:
        return None
    end = time.monotonic() + wait_s
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            if time.monotonic() >= end:
                os.close(fd)
                return None
            time.sleep(0.05)


def _ident(job: Path):
    """Which claim of a job id a dir holds: a same-digest resubmit can reclaim the path
    (rmtree + mkdir, which may reuse the inode), never the claim's own `claimed_at` /
    `submitted_ts`. None when the dir is gone."""
    try:
        ino = os.stat(job).st_ino
    except OSError:
        return None
    rec = runproc.read_json(job / "job.json") or {}
    return (ino, rec.get("claimed_at"), rec.get("submitted_ts"))


def reap(cfg, job: Path, ident=None) -> bool:
    """End a lost job's job process group (`proc.json`, else the pid `submit` spawned,
    `spawn.json`) and every gate group `proc.json` records, then write `result.json` with
    `state: "lost"` (unless the job wrote its own meanwhile). The result is written only while
    holding `job.lock`, which a job process holds from submit to its exit: a job process that
    was merely slow to start is killed first, never outlived by a `lost` it later overwrites.
    True once the job has a `result.json` (or its dir is gone); False when `job.lock` stayed
    held, or the dir is no longer the lost claim the caller found (nothing signalled or
    written).

    `ident` is the dir's `_ident` from the caller's scan. The scan may be stale by now: a
    same-digest resubmit can reclaim the path and spawn a healthy job. So, under `jobs_lock`
    (which every claim holds), the dir must still be that claim and still read `lost` before
    any signal; the leaders signalled are read in the same section. The final write checks
    the claim again.

    The caller must NOT hold `jobs_lock`: the kill graces (seconds per group) and the
    `job.lock` wait run without it, so claims never wait on a reap. `jobs_lock` is taken only
    for those checks before the signals and for the final write, which re-checks the dir (gc
    may have removed it) and `result.json`.
    Holding `job.lock` while waiting for `jobs_lock` cannot deadlock: no one holding
    `jobs_lock` ever waits for an existing job's `job.lock`."""
    stale = f"nw worker: {job.name}: the job dir was claimed again; not reaping it"
    with W.jobs_lock(cfg):
        now_ident = _ident(job)
        if now_ident is None:                   # gone (gc removed it): nothing to reap
            return True
        if ident is not None and now_ident != ident:
            print(stale, file=sys.stderr)
            return False
        ident = now_ident
        if (job / "result.json").exists():
            return True
        if _live_state(job, settings(cfg), time.time())[0] != "lost":
            print(f"nw worker: {job.name}: no longer lost; not reaping it", file=sys.stderr)
            return False
        proc = runproc.read_json(job / "proc.json") or {}
        spawn = runproc.read_json(job / "spawn.json") or {}
    grace = _kill_grace_s()
    for leader in (proc, spawn):
        # even when the leader is dead: a child left in its group (a `git` of the checkout)
        # is ended too. `group_alive` checks the leader's start ticks while its pid exists,
        # and a pgid still in use is never handed out as a new pid, so no other group is hit
        if leader.get("pid"):
            _kill_group(_leader(leader), grace, cont=True)
    for gate in proc.get("gates") or []:
        if isinstance(gate, dict):
            _kill_group(gate, grace)
    fd = _take_lock_nb(job, 1.0)
    if fd is None:
        if not job.is_dir() or (job / "result.json").exists():
            return True
        print(f"nw worker: {job.name}: job.lock is still held; not recording it lost",
              file=sys.stderr)
        return False
    try:
        with W.jobs_lock(cfg):
            if job.is_dir() and _ident(job) != ident:
                print(stale, file=sys.stderr)
                return False
            if job.is_dir() and not (job / "result.json").exists():
                own = (runproc.read_json(job / "job.json") or {}).get("state")
                if own in ("done", "error"):    # it died after its final job.json: keep it
                    _write_atomic(job / "result.json", {"state": own, "frame": None,
                                                        "reaped_at": now_iso()})
                else:
                    _write_atomic(job / "result.json", {"state": "lost", "frame": None,
                                                        "reaped_at": now_iso()})
                    _set_job_json(job, state="lost", finished_at=now_iso())
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return True


def reap_lost(cfg, errors: list | None = None) -> list[str]:
    """`gc`'s first step: reap every async job that is `lost` with no `result.json`. The scan
    runs under `jobs_lock`, the kills without it (`reap`). Caller must NOT hold `jobs_lock`.
    A job whose scan or reap fails (an unwritable dir, a full disk) is listed in `errors`
    (`{job, error}`) and the others still run."""
    errors = [] if errors is None else errors
    sett = settings(cfg)
    lost = []
    jd = W.jobs_dir(cfg)
    with W.jobs_lock(cfg):
        for job in sorted(jd.iterdir()) if jd.is_dir() else []:
            try:
                if not job.is_dir() or not _is_async(job) or (job / "result.json").exists():
                    continue
                if job_state(cfg, job, sett, locked=True)["state"] == "lost":
                    lost.append((job, _ident(job)))
            except OSError as e:
                errors.append({"job": job.name, "error": str(e)})
    reaped = []
    for job, ident in lost:
        try:
            if reap(cfg, job, ident):
                reaped.append(job.name)
        except OSError as e:
            errors.append({"job": job.name, "error": str(e)})
    return reaped


# ---- submit ----------------------------------------------------------------------------------

def _unclaim(cfg, job, frames, stage, error) -> int:
    """A claimed job that never got a job process (the bundle upload broke off, or the spawn
    failed): its dir goes, so the same header can simply be submitted again. The `error`
    frame goes to stdout only, with no `n` (there is no job to number it)."""
    frames.log = None
    frames.emit("error", stage=stage, error=error)
    try:
        with W.jobs_lock(cfg):
            shutil.rmtree(job, ignore_errors=True)
    except OSError as e:
        print(f"nw worker: could not remove {job}: {e}", file=sys.stderr)
    return W.EXIT_ERROR


def _drain(stdin, n: int) -> None:
    """Read and drop up to `n` bytes of bundle (until EOF) that no job will use. A submit
    that answers from the header alone (an idempotent or refused resubmit) still takes the
    client's whole upload, so a client that writes header + bundle before it reads stdout
    never sees its write fail (EPIPE) on a channel the worker closed early."""
    left = n
    try:
        while left > 0:
            chunk = stdin.read(min(left, 1 << 20))
            if not chunk:
                return
            left -= len(chunk)
    except (OSError, ValueError):
        return


def _spawn(cfg, job: Path, lock_fd: int) -> subprocess.Popen:
    env = {**os.environ, "NW_DATA_DIR": str(cfg["data_dir"]), LOCK_FD_ENV: str(lock_fd)}
    if cfg.get("config_dir"):
        env["NW_CONFIG_DIR"] = str(cfg["config_dir"])
    with open(job / "stderr.log", "ab") as err:
        return subprocess.Popen(
            [sys.executable, "-m", "nightwright.agentic.worker_job", str(job)], env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
            start_new_session=True, pass_fds=(lock_fd,))


def _claim(job: Path, h: dict, digest: str) -> tuple[int, dict]:
    """The claim's writes into the new dir (under `jobs_lock`): `job.lock` (flocked, the fd
    returned), `job.json`, `header.json`. When one fails, the dir goes again and the error is
    raised."""
    lock_fd = None
    try:
        lock_fd = os.open(job / "job.lock", os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        payload = h["payload"]
        job_rec = {"schema": "nw.worker.job.v1", "job_id": h["job_id"], "kind": h["kind"],
                   "campaign": h["campaign"], "spec_id": h["spec_id"],
                   "repo_key": h["repo_key"], "commit": h["commit"], "base": h["base"],
                   "attempt": h.get("attempt"), "seq": payload.get("seq"),
                   "tier": payload.get("tier"), "pid": os.getpid(),
                   "claimed_at": now_iso(), "state": "accepted", "header_digest": digest,
                   "submitted_ts": time.time()}
        _write_atomic(job / "job.json", job_rec)
        _write_atomic(job / "header.json", h)
    except OSError:
        if lock_fd is not None:
            os.close(lock_fd)
        shutil.rmtree(job, ignore_errors=True)
        raise
    return lock_fd, job_rec


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
        if h["job_id"].startswith("-"):          # it could never be named to attach/poll
            raise W.WorkerError("validate", "job_id must not start with '-'")
    except W.WorkerError as e:
        frames.emit("error", stage=e.stage, error=e.error)
        return W.EXIT_ERROR
    digest = header_digest(h)
    sett = settings(cfg)
    job = W.jobs_dir(cfg) / h["job_id"]
    existing = None
    reclaim_error = claim_error = None
    retryable = False
    with W.jobs_lock(cfg):
        if W.update_refusal(cfg):               # SPEC-0369: never start on a replaced install
            claim_error, retryable = W.UPDATING_ERROR, True
        else:
            W.jobs_dir(cfg).mkdir(parents=True, exist_ok=True)
            try:
                job.mkdir()
            except FileExistsError:
                existing = runproc.read_json(job / "job.json") or {}
                if existing.get("header_digest") == digest:
                    if "spawned_ts" not in existing and _lock_held(job) is False:
                        # its upload broke off and its submit died: no job ever ran, claim afresh
                        shutil.rmtree(job, ignore_errors=True)
                        try:
                            job.mkdir()
                            existing = None
                        except OSError as e:        # something in it could not be removed
                            reclaim_error = f"could not reclaim job {h['job_id']}: {e}"
                    else:
                        st = job_state(cfg, job, sett, locked=True)
            except OSError as e:                    # a full disk: nothing claimed
                claim_error = f"could not claim job {h['job_id']}: {e}"
            if existing is None and claim_error is None:
                try:
                    lock_fd, job_rec = _claim(job, h, digest)
                    frames.log = job / "frames.jsonl"
                except OSError as e:                # a full disk: un-claimed, no traceback
                    claim_error = f"could not claim job {h['job_id']}: {e}"
    if existing is not None or claim_error is not None:     # outside jobs_lock
        _drain(stdin, h["bundle_bytes"])        # the client's upload, so it never hits EPIPE
        if reclaim_error is not None or claim_error is not None:
            frames.emit("error", stage="claim", error=reclaim_error or claim_error,
                        **({"retryable": True} if retryable else {}))
            return W.EXIT_ERROR
        if existing.get("header_digest") == digest:     # idempotent: nothing new starts
            _print(stdout, _status(h["job_id"], st["state"], n=_last_n(job),
                                   heartbeat_age_s=st["heartbeat_age_s"]))
            return 0
        why = "with a different header" if existing.get("header_digest") else "(a gate-job)"
        frames.emit("error", stage="claim",
                    error=f"job {h['job_id']} already exists on this worker {why}")
        return W.EXIT_ERROR
    try:
        try:
            if h["bundle_bytes"]:
                W._read_exact(stdin, h["bundle_bytes"], job / "bundle")
            _renew(job, sett["job_lease_s"])     # the first lease, before the process exists
            job_rec["spawned_ts"] = time.time()
            _write_atomic(job / "job.json", job_rec)
            child = _spawn(cfg, job, lock_fd)
        except W.WorkerError as e:
            return _unclaim(cfg, job, frames, e.stage, e.error)
        except OSError as e:
            return _unclaim(cfg, job, frames, "spawn", f"could not start the job process: {e}")
    finally:
        os.close(lock_fd)                       # the job process keeps the flock
    try:                                        # the pid the kill path needs before proc.json
        _write_atomic(job / "spawn.json", {"pid": child.pid, "pgid": child.pid,
                                           "start_ticks": runproc.start_ticks(child.pid),
                                           "spawned_at": now_iso()})
    except OSError as e:
        print(f"nw worker: could not record the job process: {e}", file=sys.stderr)
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


def _gone(stdout, job_id, after: int) -> int:
    _print(stdout, _status(job_id, "unknown", n=after))
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
        if not job.is_dir():                    # removed under us (gc): nothing more to follow
            return _gone(stdout, job_id, reader.after)
        if st["state"] == "lost":
            _print(stdout, _status(job_id, "lost", heartbeat_age_s=st["heartbeat_age_s"]))
            return EXIT_LOST
        if st["state"] in ("done", "error"):    # the cursor was already past the final frame
            _print(stdout, _status(job_id, st["state"], n=reader.after))
            return 0 if st["state"] == "done" else W.EXIT_ERROR
        now = time.monotonic()
        if renewed is None or now - renewed > rewrite_every:
            if not _renew_live(cfg, job, lease_s):
                return _gone(stdout, job_id, reader.after)
            renewed = now
        time.sleep(FOLLOW_TICK_S)


def poll(cfg, job_id, after: int, renew: bool, stdout) -> int:
    """`nw worker poll JOB --from N [--renew]`: the complete frames after `N` once, then a
    `status` line. `--renew` sets `lease_until = now + job_lease_s`."""
    job = _job_or_none(cfg, job_id)
    if job is None:
        return _unknown(stdout, job_id)
    sett = settings(cfg)
    if renew and not _renew_live(cfg, job, sett["job_lease_s"]):
        return _unknown(stdout, job_id)
    st = job_state(cfg, job, sett)
    reader = _Reader(job / "frames.jsonl", after)
    for line in reader.read():
        stdout.write(line)
    _print(stdout, _status(job_id, st["state"], heartbeat_age_s=st["heartbeat_age_s"],
                           n=reader.after))
    return 0


def cancel(cfg, job_id, stdout) -> int:
    """`nw worker cancel JOB`: `cancel` file for an accepted/running job, a no-op for a finished
    one, the kill path for a lost one. The state is read (and the `cancel` file written) under
    `jobs_lock`, so it is never a half-done claim and never lands in a dir gc is removing; the
    kill path runs after it, without the lock (`reap`). Exit 1 when the job is unknown, is a
    SPEC-0336 gate-job (its stream owns it), or stayed locked through the kill path."""
    with W.jobs_lock(cfg):
        job = _job_or_none(cfg, job_id)
        if job is None:
            return _unknown(stdout, job_id)
        st = job_state(cfg, job, locked=True)["state"]
        if st in ("accepted", "running", "lost") and not _is_async(job):
            print("nw worker: a gate-job is cancelled by closing its stream",
                  file=sys.stderr)
            _print(stdout, _status(job_id, st))
            return EXIT_UNKNOWN
        if st in ("accepted", "running"):
            try:
                (job / "cancel").write_text(now_iso() + "\n", encoding="utf-8")
            except OSError as e:
                _print(stdout, _status(job_id, st, cancel="failed", error=str(e)))
                return EXIT_UNKNOWN
            _print(stdout, _status(job_id, st, cancel="requested"))
            return 0
        ident = _ident(job)
    if st == "lost":
        try:
            ok = reap(cfg, job, ident)
        except OSError as e:
            _print(stdout, _status(job_id, st, reaped=False, error=str(e)))
            return EXIT_UNKNOWN
        _print(stdout, _status(job_id, st, reaped=ok))
        return 0 if ok else EXIT_UNKNOWN
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
            _write_atomic(job / "heartbeat.json", {"ts": time.time(), "pid": os.getpid(),
                                                   "mono": time.monotonic(), "boot": _boot()})
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
                # a gate already gone has no start ticks: nothing to record (or ever signal)
                ticks = runproc.start_ticks(pid)
                recorded[pid] = {"pid": pid, "pgid": pid, "start_ticks": ticks}
            proc["gates"] = [g for g in recorded.values() if g["start_ticks"] is not None]
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
    with W.jobs_lock(cfg):
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
        elif h["kind"] == "persona":            # SPEC-0344
            from . import remote_personas as RPS
            stage = "run"
            RPS.run_job(cfg, h, job, frames, cancel)
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
        with W.jobs_lock(cfg):                  # serialized with a reader's `_mirror`
            _set_job_json(job, state=state, finished_at=now_iso(), **extra)
        W._cleanup(cfg, h, job, start_gc=False)
    except Exception as e:
        print(f"nw worker job: cleanup failed: {e}", file=sys.stderr)
    _write_atomic(job / "result.json", {"state": state, "frame": frames.last,
                                        "finished_at": now_iso()})
    stop.set()
    W._start_gc(cfg)
    return 0 if state == "done" else W.EXIT_ERROR


def _own_session(argv) -> int:
    """Run the job process again in a session of its own and wait for it. A job process started
    by hand (from a shell or a pipeline) shares the caller's process group, which the kill
    path would `killpg` once the job is lost; in its own session, its group is only its own.
    A handed-over `job.lock` fd is passed on."""
    raw = os.environ.get(LOCK_FD_ENV)
    fds = ()
    if raw and raw.isdigit():
        try:
            os.fstat(int(raw))
            fds = (int(raw),)
        except OSError:
            pass
    child = subprocess.Popen([sys.executable, "-m", "nightwright.agentic.worker_job", *argv],
                             start_new_session=True, pass_fds=fds)
    for fd in fds:
        os.close(fd)                            # the job process alone holds the flock now
    try:
        return child.wait()
    except KeyboardInterrupt:                   # the job is the worker's: it keeps running
        return 130


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    if os.getsid(0) != os.getpid():             # not `submit`'s spawn: started by hand
        return _own_session(argv)
    job = Path(argv[0])
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
            "started_ts": time.time(), "started_mono": time.monotonic(), "boot": _boot(),
            "started_at": now_iso(), "gates": []}
    if not runproc.claim(job, proc):
        print(f"nw worker job: {job} is already claimed", file=sys.stderr)
        return W.EXIT_ERROR
    return run_job(cfg, job, proc)


if __name__ == "__main__":
    sys.exit(main())
