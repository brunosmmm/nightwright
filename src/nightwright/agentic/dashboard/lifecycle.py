"""Dashboard server lifecycle (SPEC-0404): the `server.json` runtime record, its liveness reader,
the code-change watcher thread and the `agentic.dashboard.code_check_s` setting. Pure logic;
`server.py` / `cli_worker._serve_dashboard` only call it. Writes only `server.json` under
`agentic/dashboard/`. Imports no queue module except `codewatch` and `store`.

SPEC-0427 adds the service side: the `nw-dashboard.service` systemd user unit text, the
systemctl / loginctl probes and the `nw.dashboard.status.v1` document (`status`, `brief`). Every
subprocess goes through an injectable `run` so tests and `nw doctor` control it."""
from __future__ import annotations

import json
import math
import os
import re as _re
import shlex as _shlex
import shutil as _shutil
import subprocess as _subprocess
import sys
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


# ---- the systemd user unit and `nw dashboard status` (SPEC-0427) ---------------------------
#
# The unit only supervises; the exit codes are SPEC-0404's: 0 = restart onto new code (or a
# clean stop), 2 = a configuration error retrying cannot fix, 3 = no tailnet address yet.

UNIT_NAME = "nw-dashboard"
RESTART_SEC = 5                 # a code-change exit (0) is back up within ~RestartSec + startup
TAILNET_WAIT_S = 60             # `--tailnet-wait` of a tailscale unit: at most one exit 3 a minute
START_LIMIT_INTERVAL_S = 300    # a crash loop (exit 1) stops after BURST starts in INTERVAL;
START_LIMIT_BURST = 20          # code changes and the exit-3 cadence never reach it
PREVENT_EXIT = (2,)             # a refused bind / bad flag: retrying cannot fix it
SYSTEMCTL_TIMEOUT_S = 5.0
_UNIT_RE = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]*$")


def unit_name(name=None) -> str:
    """The bare unit name (no `.service`), validated: letters, digits, `_ . @ -`."""
    n = (name or UNIT_NAME).strip()
    if n.endswith(".service"):
        n = n[: -len(".service")]
    if not _UNIT_RE.match(n):
        raise ValueError(f"invalid unit name {name!r}")
    return n


def unit_dir() -> Path:
    """`$XDG_CONFIG_HOME/systemd/user` (default `~/.config/systemd/user`): the user manager's
    own unit directory."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "systemd" / "user"


def unit_path(name=None) -> Path:
    return unit_dir() / f"{unit_name(name)}.service"


def logs_command(name=None) -> str:
    return f"journalctl --user -u {unit_name(name)} -f"


def _uv_tool_nw() -> Path:
    base = os.environ.get("UV_TOOL_DIR") or os.path.join(
        os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "uv", "tools")
    return Path(base) / "nightwright" / "bin" / "nw"


def resolve_nw_bin(explicit=None, *, which=_shutil.which) -> str:
    """The `nw` the unit runs: `explicit`, else the `uv tool` install
    (`<uv tools>/nightwright/bin/nw`, the human's editable install), else `nw` on PATH with
    symlinks resolved, else the `nw` next to the running interpreter. Never a worktree's
    `.venv` by accident: `uv run` puts that first on PATH, so the uv tool path wins over PATH."""
    if explicit:
        p = Path(explicit).expanduser()
        if not (p.is_file() and os.access(p, os.X_OK)):
            raise ValueError(f"{explicit} is not an executable file")
        return str(p.absolute())
    tool = _uv_tool_nw()
    if tool.is_file() and os.access(tool, os.X_OK):
        return str(tool)
    found = which("nw")
    if found:
        return str(Path(found).resolve())
    sib = Path(sys.executable).parent / "nw"
    if sib.is_file():
        return str(sib)
    raise ValueError("no `nw` executable found; install with `uv tool install --editable "
                     "<nightwright checkout>` or pass --nw-bin")


def env_overrides() -> dict:
    """`NW_CONFIG_DIR` / `NW_DATA_DIR` for the unit, only where this shell resolves them to a
    non-default directory (the user manager has neither `NW_*` nor, usually, `XDG_*_HOME`)."""
    from ... import paths
    out = {}
    for key, now, default in (
            ("NW_CONFIG_DIR", paths.config_dir(), "~/.config/nightwright"),
            ("NW_DATA_DIR", paths.data_dir(), "~/.local/share/nightwright")):
        if os.path.abspath(now) != os.path.abspath(os.path.expanduser(default)):
            out[key] = os.path.abspath(now)
    return out


def unit_bind(bind="loopback", port=None) -> str:
    """`loopback` → `127.0.0.1:PORT`, `tailscale` → `tailscale:PORT` (default port 8791)."""
    from . import auth
    p = auth.DEFAULT_PORT if port is None else int(port)
    if not 0 < p < 65536:
        raise ValueError(f"port {port} out of range")
    if bind == "loopback":
        return f"127.0.0.1:{p}"
    if bind == "tailscale":
        return f"tailscale:{p}"
    raise ValueError(f"bind must be loopback or tailscale, not {bind!r}")


def _q(arg: str) -> str:
    """One ExecStart/Environment word: `%` and `$` escaped (systemd specifiers / expansion),
    double-quoted when it holds whitespace, a quote or a backslash."""
    s = str(arg).replace("%", "%%").replace("$", "$$")
    if s and not _re.search(r'[\s"\'\;]', s):
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def exec_args(nw_bin, bind, *, code_root=None) -> list[str]:
    args = [nw_bin, "serve", "--dashboard", "--bind", bind]
    if bind.startswith("tailscale"):
        args += ["--tailnet-wait", str(TAILNET_WAIT_S)]
    if code_root:
        args += ["--code-root", str(code_root)]
    return args


def render_unit(*, nw_bin, bind, env=None, after_tailscaled=False, code_root=None,
                name=None) -> str:
    """The `nw-dashboard.service` text. `Restart=always`: the code-change exit 0 is a restart
    onto the new code; `RestartPreventExitStatus=2` stops a configuration error; exit 3 (no
    tailnet address) is slowed in-process by `--tailnet-wait` and so never trips the start
    limit, which only a real crash loop reaches. `After=tailscaled.service` (ordering only, never
    `Requires`/`Wants`) is added when such a unit exists; a user manager cannot order against a
    system unit, so for the usual system `tailscaled` it is inert and `--tailnet-wait` is the
    real guard."""
    after = ["After=tailscaled.service"] if after_tailscaled else []
    envs = [f"Environment={_q(f'{k}={v}')}" for k, v in sorted((env or {}).items())]
    lines = [
        "# Generated by `nw dashboard install` (SPEC-0427). Re-run it to change; "
        "`nw dashboard uninstall` removes it.",
        "[Unit]",
        "Description=nightwright remote dashboard (nw serve --dashboard)",
        *after,
        f"StartLimitIntervalSec={START_LIMIT_INTERVAL_S}",
        f"StartLimitBurst={START_LIMIT_BURST}",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={' '.join(_q(a) for a in exec_args(nw_bin, bind, code_root=code_root))}",
        *envs,
        "Environment=PYTHONUNBUFFERED=1",
        "Restart=always",
        f"RestartSec={RESTART_SEC}",
        f"RestartPreventExitStatus={' '.join(str(c) for c in PREVENT_EXIT)}",
        "StandardOutput=journal",
        "StandardError=journal",
        f"SyslogIdentifier={unit_name(name)}",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def install_commands(name=None, *, restart=False) -> list[list[str]]:
    n = f"{unit_name(name)}.service"
    out = [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", n]]
    if restart:                     # already running: `enable --now` would keep the old unit
        out.append(["systemctl", "--user", "restart", n])
    return out


def uninstall_commands(name=None) -> list[list[str]]:
    n = f"{unit_name(name)}.service"
    return [["systemctl", "--user", "disable", "--now", n],
            ["systemctl", "--user", "reset-failed", n],     # while still loaded; may be a no-op
            ["rm", "-f", str(unit_path(name))],
            ["systemctl", "--user", "daemon-reload"]]


def shell_line(cmd) -> str:
    return " ".join(_shlex.quote(c) for c in cmd)


def _run(run, cmd):
    """`run(cmd)` → CompletedProcess, or None when the tool is missing or hangs. `run=None` is
    `subprocess.run`, looked up per call so tests can patch it."""
    run = run or _subprocess.run
    try:
        return run(cmd, capture_output=True, text=True, timeout=SYSTEMCTL_TIMEOUT_S)
    except (OSError, _subprocess.SubprocessError):
        return None


def tailscaled_unit_exists(run=None) -> bool:
    """A `tailscaled.service` in the user manager or the system manager."""
    for scope in (["--user"], []):
        p = _run(run, ["systemctl", *scope, "show", "tailscaled.service", "-p", "LoadState",
                       "--value"])
        if p is not None and p.returncode == 0 and p.stdout.strip() == "loaded":
            return True
    return False


SHOW_PROPS = ("LoadState", "UnitFileState", "ActiveState", "SubState", "MainPID", "NRestarts",
              "Result", "ExecMainStatus", "FragmentPath")


def systemctl_show(name=None, run=None) -> dict | None:
    """`systemctl --user show <unit>` as a dict of SHOW_PROPS; None when the user manager cannot
    be reached (no systemd, no user bus)."""
    p = _run(run, ["systemctl", "--user", "show", f"{unit_name(name)}.service",
                   "-p", ",".join(SHOW_PROPS)])
    if p is None or p.returncode != 0 or not isinstance(p.stdout, str):
        return None
    out = {}
    for line in p.stdout.splitlines():
        k, sep, v = line.partition("=")
        if sep and k in SHOW_PROPS:
            out[k] = v
    return out if "ActiveState" in out else None


def current_user() -> str:
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError):
        return os.environ.get("USER") or "$USER"


def linger_enabled(run=None, user=None) -> bool | None:
    """`loginctl show-user <user> -p Linger`: True / False, None when it cannot be read."""
    p = _run(run, ["loginctl", "show-user", user or current_user(), "-p", "Linger"])
    if p is None or p.returncode != 0 or not isinstance(p.stdout, str):
        return None
    v = p.stdout.strip().partition("=")[2].strip()
    return {"yes": True, "no": False}.get(v)


def linger_fix(user=None) -> str:
    return f"loginctl enable-linger {user or current_user()}"


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def status(cfg, *, name=None, run=None, code_root=CW.AUTO,
           query_unit=None) -> dict:
    """`nw.dashboard.status.v1`: the unit (installed / enabled / active, pid, restarts), the
    live server from `server.json`, whether it runs the checkout's current code, linger, the log
    command, and `problems` (something is wrong) vs `warnings` (worth knowing). Read-only.
    `query_unit=False` skips systemctl when the unit file does not exist (doctor, conduct-status:
    a host without the service runs no subprocess)."""
    n = unit_name(name)
    path = unit_path(n)
    installed = path.is_file()
    show = None
    if installed or query_unit:
        show = systemctl_show(n, run)
    unit = {"name": f"{n}.service", "path": str(path), "installed": installed,
            "reachable": show is not None, "enabled": None, "active": None, "state": None,
            "sub_state": None, "main_pid": None, "n_restarts": None, "result": None,
            "exit_status": None}
    if show is not None:
        loaded = show.get("LoadState") == "loaded"
        unit.update(enabled=show.get("UnitFileState") == "enabled",
                    active=show.get("ActiveState") == "active",
                    state=show.get("ActiveState"), sub_state=show.get("SubState"),
                    main_pid=_int(show.get("MainPID")) or None,
                    n_restarts=_int(show.get("NRestarts")), result=show.get("Result"),
                    exit_status=_int(show.get("ExecMainStatus")))
        if not installed and loaded:            # loaded from elsewhere (e.g. a system path)
            unit["installed"] = installed = True
            unit["path"] = show.get("FragmentPath") or str(path)
    rec = read_server_record(cfg)
    live = rec is not None and record_live(rec)
    server = None
    if live:
        server = {k: rec.get(k) for k in ("pid", "url", "bind", "started_at", "code_watch",
                                          "code_sha", "clients", "code_root")}
    code = {"watched": bool(live and rec.get("code_watch")), "running_sha": None,
            "head_sha": None, "current": None, "root": None}
    if live and rec.get("code_watch"):
        root = rec.get("code_root") or (CW.code_checkout() if code_root is CW.AUTO
                                         else code_root)
        code["root"] = None if root is None else str(root)
        code["running_sha"] = rec.get("code_sha")
        code["head_sha"] = head_sha(root) if root is not None else None
        if code["head_sha"] and code["running_sha"]:
            code["current"] = code["head_sha"] == code["running_sha"]
    linger = linger_enabled(run) if (installed or query_unit) else None
    problems, warnings = [], []
    if installed:
        if show is None:
            problems.append("cannot query `systemctl --user` (no user manager?)")
        elif not unit["active"]:
            why = f"{unit['state']}" + (f", result {unit['result']}" if unit["result"]
                                         not in (None, "success") else "") + \
                (f", last exit {unit['exit_status']}" if unit["exit_status"] else "")
            problems.append(f"installed but not active ({why}); logs: {logs_command(n)}")
        elif not live:
            problems.append("unit active but no live server.json (starting, or waiting for a "
                            f"tailnet address); logs: {logs_command(n)}")
        if show is not None and not unit["enabled"]:
            warnings.append(f"unit is not enabled (it will not start at login/boot): "
                            f"systemctl --user enable {n}.service")
        if linger is False:
            warnings.append(f"linger is off: the unit stops when you log out; run "
                            f"`{linger_fix()}`")
    else:
        warnings.append("dashboard service not installed: nw dashboard install")
    if code["current"] is False:
        problems.append(f"running code {str(code['running_sha'])[:7]} is not the checkout's "
                        f"{str(code['head_sha'])[:7]} (a code change restarts it within "
                        f"code_check_s; if it persists, the code watch is stuck)")
    return {"schema": "nw.dashboard.status.v1", "unit": unit, "running": live,
            "server": server, "code": code, "linger": linger,
            "linger_fix": linger_fix() if linger is False else None,
            "logs": logs_command(n), "ok": not problems, "problems": problems,
            "warnings": warnings}


def brief(cfg, *, run=None, name=None) -> dict | None:
    """The one-line view `conduct-status` and `doctor` show: None when there is no unit file and
    no live server (nothing to report, and no subprocess was run)."""
    st = status(cfg, name=name, run=run, query_unit=False)
    if not st["unit"]["installed"] and not st["running"]:
        return None
    return {"installed": st["unit"]["installed"], "active": st["unit"]["active"],
            "state": st["unit"]["state"], "running": st["running"],
            "url": (st["server"] or {}).get("url"), "pid": (st["server"] or {}).get("pid"),
            "linger": st["linger"], "ok": st["ok"], "problems": st["problems"],
            "warnings": [w for w in st["warnings"] if not w.startswith("dashboard service not")]}


def brief_line(b: dict) -> str:
    """`dashboard: active · http://127.0.0.1:8791 · pid 123` style line (plain text)."""
    if not b["installed"]:
        return f"dashboard: running by hand at {b['url']} (pid {b['pid']}; not a service)"
    if not b["active"]:
        return f"dashboard: installed but NOT active ({b['state']}) · nw dashboard status"
    parts = ["dashboard: active", b["url"] or "no server.json yet"]
    if b["pid"]:
        parts.append(f"pid {b['pid']}")
    parts += b["problems"] + b["warnings"]
    return " · ".join(parts)
