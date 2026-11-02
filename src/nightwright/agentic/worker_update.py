"""`nw worker update` (SPEC-0369): replace this worker's own nightwright install, as the worker
user, from a wheel sent on stdin through the forced command.

stdin is one `nw.worker.update.v1` JSON line then `size` wheel bytes; stdout is one
`nw.worker.update.result.v1` line. `uv` never writes to the ssh channel (its output goes to
`worker/update/uv.log`), runs in its own session with default signal dispositions, and the
updater ignores the hangup signals, so a dropped connection cannot abort the install half-way.
The replaced venv is the one this process runs from, so the check afterwards is a new process."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from .. import buildid
from . import worker as W

SCHEMA = "nw.worker.update.v1"
RESULT_SCHEMA = "nw.worker.update.result.v1"
EXIT_BUSY = 5
MAX_HEADER_BYTES = 64 * 1024
MAX_WHEEL_BYTES = 64 * 1024 * 1024
INSTALL_TIMEOUT_S = 900.0
VERIFY_TIMEOUT_S = 120.0
CHUNK = 1024 * 1024
WHEEL_RE = re.compile(r"^nightwright-[0-9A-Za-z.!+_]+-py3-none-any\.whl\Z")
SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")
BUILD_RE = re.compile(r"^b1:[0-9a-f]{64}\Z")
HANGUPS = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM, signal.SIGPIPE)
UV_FALLBACKS = ("~/.local/bin/uv", "/usr/local/bin/uv", "/usr/bin/uv")


class _Done(Exception):
    """An answer is ready: the result dict's fields."""

    def __init__(self, state, stage=None, error=None, **fields):
        super().__init__(error)
        self.result = {"state": state, "stage": stage, "error": error, **fields}


def _fail(stage, error, **fields):
    return _Done("failed", stage, error, **fields)


def update_dir(cfg) -> Path:
    return W.worker_dir(cfg) / "update"


def _write_atomic(path: Path, rec: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(rec), encoding="utf-8")
    os.replace(tmp, path)


def _header(stdin) -> dict:
    line = stdin.readline(MAX_HEADER_BYTES + 1)
    if len(line) > MAX_HEADER_BYTES or not line.endswith(b"\n"):
        raise _fail("header", "the header line is missing, unterminated or over 64 KiB")
    try:
        h = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise _fail("header", f"the header is not JSON: {e}")
    if not isinstance(h, dict) or h.get("schema") != SCHEMA:
        raise _fail("header", f"schema must be {SCHEMA}")
    wheel, size, sha, build = (h.get(k) for k in ("wheel", "size", "sha256", "build_id"))
    if not isinstance(wheel, str) or not WHEEL_RE.match(wheel):
        raise _fail("header", "wheel must be a nightwright-<version>-py3-none-any.whl file name")
    if not isinstance(size, int) or isinstance(size, bool) or not 1 <= size <= MAX_WHEEL_BYTES:
        raise _fail("header", f"size must be an integer in 1..{MAX_WHEEL_BYTES}")
    if not isinstance(sha, str) or not SHA256_RE.match(sha):
        raise _fail("header", "sha256 must be 64 lowercase hex digits")
    if not isinstance(build, str) or not BUILD_RE.match(build):
        raise _fail("header", "build_id must be b1:<64 hex>")
    return h


def _receive(cfg, h: dict, stdin) -> Path:
    """Stream exactly `size` bytes into `update/<wheel>.part`, checking the sha256."""
    d = update_dir(cfg)
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    for stale in d.glob("*.part"):
        stale.unlink(missing_ok=True)
    part = d / (h["wheel"] + ".part")
    left, digest = h["size"], hashlib.sha256()
    try:
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as out:
            while left:
                chunk = stdin.read(min(CHUNK, left))
                if not chunk:
                    raise _fail("receive", f"the upload ended {left} bytes short of {h['size']}")
                out.write(chunk)
                digest.update(chunk)
                left -= len(chunk)
    except OSError as e:
        raise _fail("receive", f"could not stage the wheel: {e}")
    if digest.hexdigest() != h["sha256"]:
        raise _fail("receive", "the wheel's sha256 differs from the header's")
    return part


def _validate(part: Path, h: dict) -> None:
    try:
        with zipfile.ZipFile(part) as z:
            bad = z.testzip()
            names = z.namelist()
        if bad is not None:
            raise _fail("validate", f"the wheel is corrupt at {bad}")
        if "nightwright/__init__.py" not in names:
            raise _fail("validate", "the wheel has no nightwright/__init__.py")
        got = buildid.compute_wheel(part)
    except (zipfile.BadZipFile, OSError, ValueError, RuntimeError) as e:
        raise _fail("validate", f"not a readable wheel: {e}")
    if got != h["build_id"]:
        raise _fail("validate", f"the wheel's build id {got} differs from the header's")


def find_uv(nw_argv: list[str]) -> str | None:
    """`uv` from PATH, next to the running `nw`, then the usual install places (the forced
    command's PATH is minimal)."""
    found = shutil.which("uv")
    if found:
        return found
    cands = [str(Path(nw_argv[0]).parent / "uv"), str(Path(sys.executable).parent / "uv"),
             *(os.path.expanduser(p) for p in UV_FALLBACKS)]
    return next((c for c in cands if os.path.isfile(c) and os.access(c, os.X_OK)), None)


def _clean_env() -> dict:
    return {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "PYTHONPATH")}


def _default_signals() -> None:                 # runs in the child before exec
    for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM, signal.SIGPIPE):
        signal.signal(sig, signal.SIG_DFL)


def _last_line(path: Path) -> str | None:
    try:
        lines = [ln.strip() for ln in path.read_text(encoding="utf-8", errors="replace")
                 .splitlines() if ln.strip()]
    except OSError:
        return None
    return lines[-1] if lines else None


def _install(cfg, wheel: Path, nw_argv: list[str], log: Path) -> None:
    uv = find_uv(nw_argv)
    if uv is None:
        raise _fail("install", "uv not found for this user")
    argv = [uv, "tool", "install", "--reinstall", str(wheel)]
    err = None
    try:
        with open(log, "wb") as out:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT, env=_clean_env(),
                                    start_new_session=True, restore_signals=True,
                                    preexec_fn=_default_signals)
            try:
                rc = proc.wait(timeout=INSTALL_TIMEOUT_S)
                if rc != 0:
                    err = f"uv tool install exited {rc}"
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    proc.kill()
                proc.wait()
                err = f"uv tool install timed out after {INSTALL_TIMEOUT_S:g}s"
    except OSError as e:
        err = f"could not run uv: {e}"
    try:                                        # best effort: the ssh side may be gone
        sys.stderr.write(log.read_text(encoding="utf-8", errors="replace")[-8192:])
        sys.stderr.flush()
    except (OSError, ValueError):
        pass
    if err:
        raise _fail("install", _last_line(log) or err)


def _verify(nw_argv: list[str], want: str) -> str | None:
    """The new install's version, from a fresh interpreter; `_Done` when it is not `want`."""
    try:
        p = subprocess.run([*nw_argv, "worker", "info", "--json"], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, env=_clean_env(),
                           timeout=VERIFY_TIMEOUT_S)
        info = json.loads(p.stdout) if p.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        raise _fail("verify", f"the new install did not answer `nw worker info`: {e}")
    if not isinstance(info, dict):
        tail = (p.stderr or "").strip().splitlines()[-1:] or [f"exit {p.returncode}"]
        raise _fail("verify", f"the new install did not answer `nw worker info`: {tail[0]}")
    got = info.get("build_id")
    if got != want:
        raise _fail("verify", f"the worker reports build {got}, expected {want}",
                    build_id_after=got)
    return info.get("version")


def _send(stdout, rec: dict) -> None:
    try:
        stdout.write((json.dumps(rec) + "\n").encode())
        stdout.flush()
    except (OSError, ValueError):               # the connection dropped: update.json has it
        pass


def update(cfg, force: bool, stdin, stdout, nw_argv: list[str] | None = None) -> int:
    """`nw worker update [--force]`: see the module doc. `nw_argv` is the `nw` to verify with
    (default: this worker's own, resolved now, before the install replaces it)."""
    nw_argv = list(nw_argv) if nw_argv else W.own_nw()
    res = {"schema": RESULT_SCHEMA, "state": None, "stage": None, "error": None,
           "live_jobs": [], "build_id_before": None, "build_id_after": None, "version": None,
           "forced": bool(force)}
    wd = W.worker_dir(cfg)
    lock_fd, wheel, old_handlers, holding = None, None, {}, False
    try:
        h = _header(stdin)
        wd.mkdir(parents=True, exist_ok=True)
        # held from here to the end, so a second update cannot empty the staging dir
        lock_fd = os.open(wd / "update.lock", os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise _Done("busy", "lock", "another update is running")
        holding = True
        wheel = _receive(cfg, h, stdin)
        _validate(wheel, h)
        staged = wheel.with_name(h["wheel"])    # uv wants a `.whl` name
        os.replace(wheel, staged)
        wheel = staged

        for sig in HANGUPS:                 # a dropped connection must not stop the wait
            try:
                old_handlers[sig] = signal.signal(sig, signal.SIG_IGN)
            except ValueError:              # not the main thread
                break
        with W.jobs_lock(cfg):
            live = sorted(W._live_jobs(cfg)[0])
            if live and not force:
                raise _Done("busy", None, f"{len(live)} job(s) are live", live_jobs=live)
            res["live_jobs"] = live
            _write_atomic(wd / "updating", {"pid": os.getpid(),
                                            "install_started_at": time.time(),
                                            "build_id_before": buildid.local()})
        res["build_id_before"] = buildid.local()
        if res["build_id_before"] == h["build_id"]:
            raise _Done("unchanged", build_id_after=res["build_id_before"])
        _run_install(cfg, h, wheel, nw_argv, res)
    except _Done as d:
        res.update(d.result)
    except Exception as e:                      # never a traceback on the ssh channel
        res.update(state="failed", stage=res["stage"] or "install",
                   error=f"{type(e).__name__}: {e}")
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        if holding:                             # never touch a running update's files
            d = update_dir(cfg)
            if wheel is not None:
                wheel.unlink(missing_ok=True)
            for stale in d.glob("*.part") if d.is_dir() else ():
                stale.unlink(missing_ok=True)
            (wd / "updating").unlink(missing_ok=True)
        if lock_fd is not None:
            os.close(lock_fd)
    _send(stdout, res)
    return {"updated": 0, "unchanged": 0, "busy": EXIT_BUSY}.get(res["state"], W.EXIT_ERROR)


def _run_install(cfg, h: dict, wheel: Path, nw_argv: list[str], res: dict) -> None:
    """Steps 6 and 7: install, then verify in a new process; `update.json` keeps the result."""
    wd = W.worker_dir(cfg)
    started = time.time()
    _write_atomic(wd / "update.json", {"install_started_at": started, "state": "installing"})
    try:
        _install(cfg, wheel, nw_argv, update_dir(cfg) / "uv.log")
        res["version"] = _verify(nw_argv, h["build_id"])
        raise _Done("updated", build_id_after=h["build_id"], version=res["version"])
    except _Done as d:
        saved = {**res, **d.result, "install_started_at": started}
        try:
            _write_atomic(wd / "update.json", saved)
        except OSError as e:
            print(f"nw worker update: could not save update.json: {e}", file=sys.stderr)
        raise
