"""herdr watcher auto-start (SPEC-0365, epic SPEC-0360): the conductor's event follower
supervises `nw herdr watch` as a detached background child, so the human never starts it.

The follower's stdout is the conductor's Monitor event stream: nothing here ever writes to the
follower's stdout or stderr. The child's output goes to `herdr_watch.log`; the supervisor's own
notes go there too. Every supervisor step is best effort and never raises into the follower.

This module does not import `agentic/herdr.py` (epic invariant 2: the event stream never does);
it repeats the two-line `HERDR_ENV` / `which herdr` gate. The watcher process itself writes
`herdr_watch.json` (pid + start ticks) once it holds the `herdr` reader lock."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import store

SCHEMA = "nw.herdr.watch.v1"
BACKOFF_MIN_S = 5.0
BACKOFF_MAX_S = 300.0
STABLE_S = 300.0         # a child that ran this long resets the backoff
RECHECK_S = 5.0          # while another watcher runs, look again this often
ERRORS_MAX = 5           # consecutive supervisor failures before it turns itself off
LOG_MAX_BYTES = 5 * 1024 * 1024
_OFF = {"0", "false", "no", "off"}


def _dir(cfg) -> Path:
    from .conductor_cost import conductor_dir
    return conductor_dir(cfg)


def log_path(cfg) -> Path:
    return _dir(cfg) / "herdr_watch.log"


def state_path(cfg) -> Path:
    return _dir(cfg) / "herdr_watch.json"


def auto_watch_enabled(cfg) -> bool:
    """`agentic.herdr.auto_watch`, default on; false / 0 / "false" / "off" / "no" turn it off."""
    v = (((cfg or {}).get("agentic") or {}).get("herdr") or {}).get("auto_watch", True)
    if isinstance(v, str):
        return v.strip().lower() not in _OFF
    return v is None or bool(v)


def gate(cfg, env=None) -> str | None:
    """Why the follower does not start a watcher (one line), or None when it should."""
    env = os.environ if env is None else env
    if not auto_watch_enabled(cfg):
        return "agentic.herdr.auto_watch is off"
    if env.get("HERDR_ENV") != "1":
        return "not inside a herdr pane (HERDR_ENV is not 1)"
    if shutil.which("herdr", path=env.get("PATH")) is None:
        return "`herdr` is not on PATH"
    return None


# ---- liveness / state file --------------------------------------------------------------

def start_ticks(pid) -> int | None:
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text()
        return int(raw.rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError, TypeError):
        return None


def alive(pid, ticks=None) -> bool:
    """`pid` runs and, when `ticks` is known, is the same process (same /proc start time)."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return False
    if ticks is None:
        return True
    now = start_ticks(pid)
    return now is None or now == ticks


def parse_parent(text) -> tuple[int, int | None] | None:
    """`PID:TICKS` (or `PID`) -> (pid, ticks); None when malformed."""
    pid, _, ticks = str(text or "").partition(":")
    try:
        return int(pid), (int(ticks) if ticks else None)
    except ValueError:
        return None


def read_state(cfg) -> dict | None:
    try:
        data = json.loads(state_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_state(cfg, mode, parent=None) -> dict:
    """Called by the watcher once it holds the `herdr` reader lock."""
    pid = os.getpid()
    rec = {"pid": pid, "start_ticks": start_ticks(pid), "mode": mode,
           "parent_pid": parent[0] if parent else None,
           "parent_start_ticks": parent[1] if parent else None,
           "started_at": store.now_iso(), "log": str(log_path(cfg)) if mode == "auto" else None}
    path = state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{pid}.tmp")
    tmp.write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return rec


def clear_state(cfg) -> None:
    """Remove the state file only when it is still this process's."""
    rec = read_state(cfg)
    if rec and rec.get("pid") == os.getpid():
        try:
            state_path(cfg).unlink()
        except OSError:
            pass


def live_state(cfg) -> dict | None:
    rec = read_state(cfg)
    if rec and alive(rec.get("pid"), rec.get("start_ticks")):
        return rec
    return None


def status(cfg) -> dict:
    """`nw.herdr.watch.v1`: from the state file only (pid + start ticks; no lock probe)."""
    rec = live_state(cfg) or {}
    return {"schema": SCHEMA, "auto_watch": auto_watch_enabled(cfg), "running": bool(rec),
            "pid": rec.get("pid"), "mode": rec.get("mode"),
            "parent_pid": rec.get("parent_pid"), "started_at": rec.get("started_at"),
            "log": str(log_path(cfg))}


def status_line(st) -> str:
    if st.get("running"):
        who = f"pid {st['pid']} ({st.get('mode') or '?'})"
        return f"herdr watcher: {who} · log {st['log']}"
    off = "" if st.get("auto_watch") else " (auto_watch off)"
    return f"herdr watcher: not running{off} · log {st['log']}"


# ---- the supervisor ------------------------------------------------------------------------

def watcher_argv(pid, ticks) -> list[str]:
    parent = f"{pid}:{ticks}" if ticks is not None else str(pid)
    return [sys.executable, "-m", "nightwright", "herdr", "watch", "--parent", parent]


def child_env(cfg, env=None) -> dict:
    from .. import paths
    out = dict(os.environ if env is None else env)
    out["NW_CONFIG_DIR"] = str(cfg.get("config_dir") or paths.config_dir())
    out["NW_DATA_DIR"] = str(cfg.get("data_dir") or paths.data_dir())
    out["PYTHONUNBUFFERED"] = "1"
    return out


def _reader_held(cfg) -> bool:
    from . import events as E
    return bool(E.reader_holder(cfg, "herdr"))


class Supervisor:
    """Ticked by the conductor follower once per poll. Starts one watcher child, restarts it
    with backoff, waits while another watcher runs, SIGTERMs only its own child on `stop()`.
    No method raises; nothing is written anywhere but the log."""

    def __init__(self, cfg, *, env=None, clock=time.monotonic, popen=subprocess.Popen,
                 holder=None):
        self.cfg = cfg
        self.env = os.environ if env is None else env
        self.clock = clock
        self.popen = popen
        self.holder = holder or (lambda: _reader_held(cfg))
        self.active = False
        self.child = None
        self.started_at = 0.0
        self.next_start = 0.0
        self.backoff = BACKOFF_MIN_S
        self.errors = 0
        self.spawns = 0
        self.waiting_on = None                       # the other watcher's pid we noted
        self.pid = os.getpid()
        self.ticks = start_ticks(self.pid)

    def start(self) -> bool:
        """Arm when the gate passes; a gated supervisor stays inert (no file, no log)."""
        try:
            self.active = gate(self.cfg, self.env) is None
        except Exception:
            self.active = False
        return self.active

    def log(self, msg) -> None:
        try:
            path = log_path(self.cfg)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{store.now_iso()} nw herdr supervisor (follower pid {self.pid}): "
                        f"{msg}\n")
        except Exception:
            pass

    def tick(self) -> None:
        if not self.active:
            return
        try:
            self._tick()
        except Exception as e:                      # noqa: BLE001 - best effort by design
            self.errors += 1
            self.log(f"error: {type(e).__name__}: {e}")
            self.next_start = self.clock() + self.backoff
            self.backoff = min(self.backoff * 2, BACKOFF_MAX_S)
            if self.errors >= ERRORS_MAX:
                self.active = False
                self.log(f"{ERRORS_MAX} errors in a row; auto-start off until the follower "
                         f"restarts")

    def _tick(self) -> None:
        now = self.clock()
        if self.child is not None:
            rc = self.child.poll()
            if rc is None:
                return
            ran = now - self.started_at
            if ran >= STABLE_S:
                self.backoff = BACKOFF_MIN_S
            self.log(f"watcher pid {self.child.pid} exited {rc} after {ran:.0f}s; "
                     f"next start in {self.backoff:g}s")
            self.child = None
            self.next_start = now + self.backoff
            self.backoff = min(self.backoff * 2, BACKOFF_MAX_S)
            return
        if now < self.next_start:
            return
        other = live_state(self.cfg)
        if other is not None or self.holder():
            opid = (other or {}).get("pid") or "?"
            if self.waiting_on != opid:
                self.log(f"a watcher already runs (pid {opid}); not starting another")
                self.waiting_on = opid
            self.next_start = now + RECHECK_S
            return
        self.waiting_on = None
        self._spawn(now)

    def _spawn(self, now) -> None:
        path = log_path(self.cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.stat().st_size > LOG_MAX_BYTES:
                os.replace(path, path.with_name(path.name + ".1"))
        except OSError:
            pass
        argv = watcher_argv(self.pid, self.ticks)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            self.next_start = now + self.backoff      # a failing spawn backs off too
            self.child = self.popen(argv, stdin=subprocess.DEVNULL, stdout=fd,
                                    stderr=subprocess.STDOUT, env=child_env(self.cfg, self.env),
                                    cwd=str(path.parent), close_fds=True,
                                    start_new_session=True)
        finally:
            os.close(fd)
        self.started_at = now
        self.spawns += 1
        self.errors = 0                              # "in a row": a spawn that worked resets
        self.log(f"started watcher pid {self.child.pid}")

    def stop(self) -> None:
        """SIGTERM our own child if it still runs; never waits, never raises."""
        try:
            child, self.child = self.child, None
            self.active = False
            if child is not None and child.poll() is None:
                child.send_signal(signal.SIGTERM)
                self.log(f"follower exiting; stopped watcher pid {child.pid}")
        except Exception:
            pass

    def as_stop_hook(self):
        """For `events.follow(stop=...)`: tick, never ask the follower to stop."""
        def _hook():
            self.tick()
            return False
        return _hook
