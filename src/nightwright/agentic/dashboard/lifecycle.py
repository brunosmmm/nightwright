"""Dashboard server lifecycle (SPEC-0404): the `server.json` runtime record, its liveness reader,
the code-change watcher thread and the `agentic.dashboard.code_check_s` setting. Pure logic;
`server.py` / `cli_worker._serve_dashboard` only call it. Writes only `server.json` under
`agentic/dashboard/`. Imports no queue module except `codewatch` and `store`."""
from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path

from .. import codewatch as CW
from .. import store
from . import dashboard_dir

SCHEMA = "nw.dashboard.server.v1"
DEFAULT_CODE_CHECK_S = 10
CLIENTS_MIN_S = 5.0                 # `clients` / `updated_at` are rewritten at most this often


def read_path(cfg) -> Path:
    """Where `server.json` lives, without creating or chmod-ing anything: for readers, which may
    run in a process that is not the server (`dashboard_dir` is the server's)."""
    return store.agentic_dir(cfg) / "dashboard" / "server.json"


def server_json_path(cfg) -> Path:
    """The writer's path: creates the 0700 `agentic/dashboard/` directory."""
    return dashboard_dir(cfg) / "server.json"


def start_ticks(pid) -> int | None:
    """Field 22 of `/proc/<pid>/stat`, None where `/proc` is not available."""
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text()
        return int(raw.rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError, TypeError):
        return None


def _pid_alive(pid) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def record_live(rec) -> bool:
    """True when `rec` names a running process that is the one that wrote it: `pid` alive and
    its `/proc` start ticks equal `ticks` (a crashed server's file, or a reused pid, reads as
    not running). Without `/proc` the pid check alone; where `/proc` is readable a record
    with no `ticks` is not live (it cannot prove the pid is still the server)."""
    if not isinstance(rec, dict) or not _pid_alive(rec.get("pid")):
        return False
    want, now = rec.get("ticks"), start_ticks(rec["pid"])
    return now is None or want == now


def read_server_record(cfg) -> dict | None:
    """The parsed `server.json`; None for a missing, unreadable or non-object file."""
    try:
        rec = json.loads(read_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def write_server_record(cfg, rec: dict) -> dict:
    """Atomic (temp file in the same directory + rename), 0600: no reader sees a partial file."""
    p = server_json_path(cfg)
    tmp = p.with_name(f".server.json.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, (json.dumps(rec) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, p)
    return rec


def remove_server_record(cfg) -> None:
    """Delete `server.json` only when it names this process."""
    rec = read_server_record(cfg)
    if rec is not None and rec.get("pid") == os.getpid():
        read_path(cfg).unlink(missing_ok=True)


def code_check_s(cfg) -> float:
    """`agentic.dashboard.code_check_s`: seconds between code-change checks (default 10; 0 turns
    the watcher off). A non-number, a negative or a non-finite value falls back to the default."""
    ag = cfg.get("agentic")
    raw = ag.get("dashboard") if isinstance(ag, dict) else None
    v = raw.get("code_check_s", DEFAULT_CODE_CHECK_S) if isinstance(raw, dict) \
        else DEFAULT_CODE_CHECK_S
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
        return DEFAULT_CODE_CHECK_S
    return v


class CodeWatchThread(threading.Thread):
    """Polls a `codewatch.Watcher` once per `poll_s`; on a code change calls `on_change(change)`
    exactly once and ends. The baseline sha is taken at construction, not when the thread runs."""

    def __init__(self, stop_event, on_change, check_s, root=CW.AUTO, poll_s=1.0):
        super().__init__(name="dashboard-codewatch", daemon=True)
        self.stop_event, self.on_change, self.poll_s = stop_event, on_change, poll_s
        self.root = CW.code_checkout() if root is CW.AUTO else root
        self.watcher = CW.Watcher(self.root, check_s)

    @property
    def enabled(self) -> bool:
        return self.watcher.enabled

    @property
    def sha(self) -> str | None:
        return self.watcher.sha

    def run(self):
        while not self.stop_event.wait(self.poll_s):
            change = self.watcher.poll(time.monotonic())
            if change:
                self.on_change(change)
                return


def head_sha(root) -> str | None:
    """`git rev-parse HEAD` of the watched checkout; None without one or when git fails."""
    return CW._git_out(root, "rev-parse", "HEAD") if root is not None else None


def code_changed_line(change: dict) -> str:
    return (f"code_changed {str(change.get('from_sha'))[:7]}..{str(change.get('to_sha'))[:7]} "
            f"skills={','.join(change.get('skills_changed') or []) or '-'}"
            f"{' diff_failed' if change.get('diff_failed') else ''} (exiting 0 to restart)")


def build_record(server, *, code_watch: bool, code_sha: str | None, bind: str) -> dict:
    """The `server.json` document for a bound `DashboardServer`."""
    host, port = server.server_address[:2]
    h = f"[{host}]" if ":" in host else host
    now = store.now_iso()
    return {"schema": SCHEMA, "pid": os.getpid(), "ticks": start_ticks(os.getpid()),
            "url": f"http://{h}:{port}", "host": host, "port": port, "bind": bind,
            "scheme": "http", "started_at": now, "code_sha": code_sha,
            "code_watch": code_watch, "clients": 0, "updated_at": now,
            "auth": getattr(server, "mode", "local")}            # SPEC-0428: local | whois


class RecordRefresher:
    """Keeps `clients` / `updated_at` in `server.json` current: `tick()` polls `client_count()`
    and rewrites the record only when the count changed and at least `min_s` passed since the
    last rewrite. An idle server rewrites nothing; no queue read happens here."""

    def __init__(self, cfg, rec, client_count, clock=time.monotonic, min_s=CLIENTS_MIN_S):
        self.cfg, self.rec, self.client_count = cfg, dict(rec), client_count
        self.clock, self.min_s = clock, min_s
        self.last_write = clock()
        self.writes = 0

    def tick(self) -> bool:
        n = int(self.client_count() or 0)
        if n == self.rec.get("clients") or self.clock() - self.last_write < self.min_s:
            return False
        cur = read_server_record(self.cfg)
        if cur is None or cur.get("pid") != os.getpid():        # taken over or removed: leave it
            return False
        self.rec.update(clients=n, updated_at=store.now_iso())
        write_server_record(self.cfg, self.rec)
        self.last_write = self.clock()
        self.writes += 1
        return True
