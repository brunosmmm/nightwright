"""Detached persona-run wrapper and liveness helpers (SPEC-0225).

`python -m wt.agentic.runproc <run_dir>` is started by `ClaudeRunner` in its own session. It
claims the run dir (`proc.json`, created `O_EXCL`), starts `claude -p` in a process group of
its own, streams stdout / stderr into the run dir, enforces the timeout and writes `exit.json`
atomically. Everything it does is recorded in files, so a runner that restarts can adopt the
run. The helpers below read those files to decide whether a run is alive."""
from __future__ import annotations

import datetime as dt
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

GRACE_S = float(os.environ.get("WT_RUNPROC_GRACE_S") or 10.0)   # TERM -> KILL grace (tests shorten it)


def child_env(extra=None, environ=None) -> dict:
    """The environment of a persona child: `environ` (default `os.environ`) plus `extra`.
    SPEC-0319: inside a launched conductor/steward session the launcher's keys
    (`WT_LAUNCH_KEYS`) and any ambient effort pin are dropped, so a campaign runner gets Claude
    Code's defaults and its own `--effort`. Keys in `extra` always win."""
    env = dict(os.environ if environ is None else environ)
    launch = env.pop("WT_LAUNCH_KEYS", None)
    if launch is not None:
        for key in [k.strip() for k in launch.split(",") if k.strip()] + ["CLAUDE_CODE_EFFORT_LEVEL"]:
            env.pop(key, None)
    return {**env, **(extra or {})}


def _now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_json_atomic(path, data) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def start_ticks(pid: int) -> int | None:
    """Field 22 of `/proc/<pid>/stat` (process start time), None off Linux or when gone."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return int(raw.rsplit(")", 1)[1].split()[19])


def _proc_state(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return raw.rsplit(")", 1)[1].split()[0]


def claim(run_dir, payload: dict) -> bool:
    """Create `proc.json` exclusively; False when someone else holds the claim. The payload is
    written to a temp file first and hard-linked into place, so `proc.json` never exists empty
    or partial (`link` fails with FileExistsError when the claim is taken)."""
    run_dir = Path(run_dir)
    path = run_dir / "proc.json"
    tmp = run_dir / f".proc.json.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            return False
    finally:
        tmp.unlink(missing_ok=True)
    dfd = os.open(run_dir, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    return True


def wrapper_alive(proc: dict | None) -> bool:
    if not proc or not proc.get("wrapper_pid"):
        return False
    pid = int(proc["wrapper_pid"])
    ticks = proc.get("wrapper_start_ticks")
    state = _proc_state(pid)
    if state is None:
        if os.path.isdir("/proc/self"):
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    if state == "Z":
        return False
    return ticks is None or start_ticks(pid) == ticks


def group_alive(child: dict | None) -> bool:
    if not child or not child.get("pgid"):
        return False
    pid, ticks = child.get("pid"), child.get("start_ticks")
    if pid and ticks is not None:
        now = start_ticks(int(pid))
        if now is not None and now != ticks:       # the leader's pid was reused
            return False
    try:
        os.killpg(int(child["pgid"]), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return not _group_all_zombies(int(child["pgid"]))


def _group_all_zombies(pgid: int) -> bool:
    """True when every process of the group is a zombie (`/proc` only; else False)."""
    if not os.path.isdir("/proc/self"):
        return False
    seen = False
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            raw = Path(f"/proc/{entry}/stat").read_text()
        except OSError:
            continue
        fields = raw.rsplit(")", 1)[1].split()
        if int(fields[2]) == pgid:
            seen = True
            if fields[0] != "Z":
                return False
    return seen


def run_status(run_dir) -> str:
    """`finished` | `running` | `orphan_group` | `vanished` | `orphaned` | `unclaimed`."""
    run_dir = Path(run_dir)
    if (run_dir / "exit.json").is_file():
        return "finished"
    proc = read_json(run_dir / "proc.json")
    if proc is None:
        return "unclaimed"
    if proc.get("orphaned"):
        return "orphaned"
    if wrapper_alive(proc):
        return "running"
    if group_alive(read_json(run_dir / "child.json")):
        return "orphan_group"
    return "vanished"


def terminate_run(run_dir, *, kill: bool = False) -> bool:
    """Send TERM (or KILL) to a live run: the wrapper forwards TERM to the child's group.
    Returns True when something was signalled."""
    run_dir = Path(run_dir)
    if (run_dir / "exit.json").is_file():
        return False
    sig = signal.SIGKILL if kill else signal.SIGTERM
    sent = False
    child = read_json(run_dir / "child.json")
    if kill and group_alive(child):
        try:
            os.killpg(int(child["pgid"]), sig)
            sent = True
        except OSError:
            pass
    proc = read_json(run_dir / "proc.json")
    if proc and wrapper_alive(proc) and not kill:
        try:
            os.kill(int(proc["wrapper_pid"]), sig)
            sent = True
        except OSError:
            pass
    elif proc and not kill and not proc.get("orphaned") and group_alive(child):
        # the wrapper is gone: TERM the group ourselves, and leave a mark so the synthesized
        # `exit.json` says `killed_by: "signal"` (the run is interrupted, not an error)
        try:
            write_json_atomic(run_dir / "term.json", {"ts": time.time()})
            os.killpg(int(child["pgid"]), sig)
            sent = True
        except OSError:
            pass
    return sent


def kill_group(child: dict | None, *, grace: float = GRACE_S) -> None:
    """TERM the child's group, wait up to `grace`, then KILL."""
    if not child or not child.get("pgid"):
        return
    pgid = int(child["pgid"])
    for sig, wait_s in ((signal.SIGTERM, grace), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(pgid, sig)
        except OSError:
            return
        end = time.monotonic() + wait_s
        while time.monotonic() < end:
            if not group_alive(child):
                return
            time.sleep(0.05)


# ---- the wrapper process ------------------------------------------------------------------

def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    run_dir = Path(argv[0])
    state = {"term": False, "child": None, "term_at": None}

    def on_term(_signum, _frame):
        state["term"] = True
        state["term_at"] = state["term_at"] or time.monotonic()
        child = state["child"]
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except OSError:
                pass

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    me = os.getpid()
    if not claim(run_dir, {"wrapper_pid": me, "wrapper_start_ticks": start_ticks(me),
                           "host": socket.gethostname(), "started_at": _now_iso()}):
        os.write(2, f"run dir already claimed: {run_dir}\n".encode())
        return 3
    began = time.monotonic()
    exit_path = run_dir / "exit.json"

    def finish(code, *, timed_out=False, killed_by=None, sig=None, **extra):
        write_json_atomic(exit_path, {
            "exit_code": code, "timed_out": timed_out, "killed_by": killed_by, "signal": sig,
            "ended_at": _now_iso(), "duration_s": round(time.monotonic() - began, 3),
            "synthesized": False, **extra})

    try:
        launch = json.loads((run_dir / "launch.json").read_text(encoding="utf-8"))
        req = json.loads((run_dir / "request.json").read_text(encoding="utf-8"))
        timeout_s = float(req.get("timeout_s") or 0) or None
        env = child_env(launch.get("env"))
        if state["term"]:
            finish(-1, killed_by="signal", sig=signal.SIGTERM)
            return 0
        try:
            with open(run_dir / "prompt.md", "rb") as stdin, \
                    open(run_dir / "result.json", "wb") as out, \
                    open(run_dir / "stderr.log", "wb") as err:
                child = subprocess.Popen(launch["argv"], stdin=stdin, stdout=out, stderr=err,
                                         cwd=launch.get("cwd"), env=env, process_group=0)
        except OSError as e:
            (run_dir / "stderr.log").write_text(str(e), encoding="utf-8")
            finish(-1, error=str(e))
            return 0
        state["child"] = child
        write_json_atomic(run_dir / "child.json", {
            "pid": child.pid, "pgid": child.pid, "start_ticks": start_ticks(child.pid)})
        if state["term"]:
            on_term(signal.SIGTERM, None)
        killed_by, timed_out, kill_at, kill_sent = None, False, None, False
        deadline = began + timeout_s if timeout_s else None
        while child.poll() is None:
            now = time.monotonic()
            if deadline and now >= deadline and not timed_out:
                timed_out, killed_by, kill_at = True, "timeout", now + GRACE_S
                _killpg(child.pid, signal.SIGTERM)
            if state["term_at"] and kill_at is None:
                kill_at = state["term_at"] + GRACE_S
            if kill_at and now >= kill_at and not kill_sent:
                kill_sent = True
                _killpg(child.pid, signal.SIGKILL)
            time.sleep(0.05)
        rc = child.returncode
        _killpg(child.pid, signal.SIGKILL) if (timed_out or state["term"]) else None
        sig = -rc if rc < 0 else None
        if killed_by is None and state["term"]:     # we forwarded a TERM: however the child ended
            killed_by = "signal"
            sig = sig or signal.SIGTERM
        elif killed_by is None and sig is not None:
            killed_by = "external"
        finish(-1 if rc < 0 else rc, timed_out=timed_out, killed_by=killed_by, sig=sig)
    except BaseException as e:                       # leave a diagnosis, never a silent death
        import traceback
        traceback.print_exc()
        if not exit_path.exists():
            error = f"wrapper failed: {e!r}"
            child = state.get("child")
            if child is not None and child.poll() is None:
                # SPEC-0262: never leave `claude` running behind a written `exit.json`
                _killpg(child.pid, signal.SIGTERM)
                end = time.monotonic() + GRACE_S
                while child.poll() is None and time.monotonic() < end:
                    time.sleep(0.05)
                _killpg(child.pid, signal.SIGKILL)
                error += "; child process group killed"
            finish(-1, error=error)
        return 1
    return 0


def _killpg(pgid, sig) -> None:
    try:
        os.killpg(pgid, sig)
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
