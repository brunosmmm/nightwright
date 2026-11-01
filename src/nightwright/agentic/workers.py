"""Worker registry (SPEC-0337): the coordinator's list of remote gate workers, kept in
`<agentic_dir>/workers.json` (`nw.workers.v1`). Registry order is preference order. Every write
goes through `update`, which takes an exclusive flock on `workers.json.lock`, re-reads the file,
applies one change and writes atomically, so concurrent writers (the `nw workers` CLI and the
cooldown update of SPEC-0338) never lose an update. `load` never raises and never writes events:
a missing, empty or unreadable file means no workers, and the runner's `ConfigWatch` reports an
unreadable one (`read_strict` is the parse it uses)."""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path

from ..export import _atomic_write
from .store import agentic_dir

SCHEMA = "nw.workers.v1"
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")
DEST_RE = re.compile(r"[A-Za-z0-9._@:-]+")
NW_CMD_RE = re.compile(r"[A-Za-z0-9._/~+-]+")
IDENTITY_RE = re.compile(r"/[A-Za-z0-9._/+-]+")  # an absolute private-key path (SPEC-0354)
MIN_SLOTS, MAX_SLOTS = 1, 16
DEFAULT_KINDS = ["gate"]
CHECK_TIMEOUT_S = 30.0
LOCAL = None                                    # the local pool's name in `gate_pools`/`acquire`


def path(cfg) -> Path:
    return agentic_dir(cfg) / "workers.json"


SOCK_MAX = 107                                  # sockaddr_un.sun_path holds 107 bytes plus the NUL
CTL_SUFFIX = 1 + 40 + 17                        # "/" + %C (40 hex) + OpenSSH's ".<16 random>" temp name
TAG_LEN = 1 + 8                                 # "-<8 hex>": an identity record's socket tag (SPEC-0354)


def _fits(d, extra=0) -> bool:
    return len(os.fsencode(str(d))) + CTL_SUFFIX + extra <= SOCK_MAX


def _ssh_dir_candidates(cfg) -> list[Path]:
    """Where the ControlPath sockets may live, best first: `<agentic_dir>/ssh/`, then (when that
    path is too long for a Unix socket) a short private dir under `$XDG_RUNTIME_DIR` or `/tmp`,
    named from a hash of the agentic dir so two data dirs never share sockets."""
    tag = hashlib.sha1(os.fsencode(str(agentic_dir(cfg)))).hexdigest()[:8]
    out = [agentic_dir(cfg) / "ssh"]
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        out.append(Path(xdg) / f"nw-ssh-{tag}")
    out.append(Path("/tmp") / f"nw-ssh-{os.getuid()}-{tag}")
    return out


def ssh_dir(cfg, extra=0) -> Path | None:
    """The directory for ControlPath sockets: the first candidate short enough for a Unix
    socket (with `extra` more bytes in the socket name), `None` when none is (ssh then runs
    without connection sharing)."""
    return next((d for d in _ssh_dir_candidates(cfg) if _fits(d, extra)), None)


def ensure_ssh_dir(cfg, extra=0) -> Path | None:
    """`ssh_dir(cfg, extra)` created with mode 0700 (and refused with `OSError` if it exists
    but is a symlink or belongs to someone else, as a shared /tmp path could); `None` when
    there is no usable directory."""
    d = ssh_dir(cfg, extra)
    if d is None:
        return None
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = d.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise OSError(f"{d} is not a directory owned by this user")
    d.chmod(0o700)
    return d


def identity_tag(identity: str) -> str:
    """The ControlPath tag of an `identity` record: `%C` hashes only user, host and port, so
    without it a record with a dedicated key could ride a master opened earlier with another
    (unrestricted) key for the same destination."""
    return hashlib.sha1(os.fsencode(identity)).hexdigest()[:TAG_LEN - 1]


def default_ssh_argv(cfg, tag=None) -> list[str]:
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]
    d = ssh_dir(cfg, TAG_LEN if tag else 0)
    if d is not None:
        argv += ["-o", "ControlMaster=auto",
                 "-o", f"ControlPath={d}/%C" + (f"-{tag}" if tag else ""),
                 "-o", "ControlPersist=120"]
    return argv


def _check_record(rec) -> None:
    if not isinstance(rec, dict):
        raise ValueError(f"worker entry is not a mapping (got {type(rec).__name__})")
    name = rec.get("name")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ValueError(f"worker name {name!r} is invalid")
    ssh = rec.get("ssh")
    if not isinstance(ssh, str) or not DEST_RE.fullmatch(ssh) or ssh.startswith("-"):
        raise ValueError(f"worker {name}: ssh destination {ssh!r} is invalid")
    nw = rec.get("nw")
    if nw is not None and (not isinstance(nw, str) or not NW_CMD_RE.fullmatch(nw)
                           or nw.startswith("-")):
        raise ValueError(f"worker {name}: nw command {nw!r} is invalid")
    ident = rec.get("identity")
    if ident is not None and not identity_ok(ident):
        raise ValueError(f"worker {name}: identity {ident!r} is invalid")


def identity_ok(value) -> bool:
    """An `identity` (SPEC-0354): an absolute path to a private key, never option-like."""
    return isinstance(value, str) and bool(IDENTITY_RE.fullmatch(value))


def _parse(text: str) -> list[dict]:
    """The raw worker records of a registry file's text; `ValueError` when it is not a valid
    `nw.workers.v1` document. Empty text means no workers."""
    if not text.strip():
        return []
    doc = json.loads(text)
    if not isinstance(doc, dict):
        raise ValueError(f"not a mapping (got {type(doc).__name__})")
    if doc.get("schema") != SCHEMA:
        raise ValueError(f"schema is {doc.get('schema')!r}, not {SCHEMA!r}")
    workers = doc.get("workers")
    if not isinstance(workers, list):
        raise ValueError("`workers` is not a list")
    seen = set()
    for rec in workers:
        _check_record(rec)
        if rec["name"] in seen:
            raise ValueError(f"worker {rec['name']!r} appears twice")
        seen.add(rec["name"])
    return workers


def read_file(p) -> list[dict]:
    """The raw records of the registry file `p`; a missing file is no workers, an unreadable
    one raises (`ValueError` for bad JSON/schema, `OSError` for a read failure)."""
    try:
        text = Path(p).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return _parse(text)


def read_strict(cfg) -> list[dict]:
    return read_file(path(cfg))


def normalize(cfg, rec: dict) -> dict:
    """A raw record with the defaults filled in (`ssh_argv` included, never stored by `add`).
    A record with an `identity` (SPEC-0354) gets `-i <identity> -o IdentitiesOnly=yes`
    appended, so every connection to that worker offers its dedicated key first, and its
    default ControlPath is tagged with that key (`identity_tag`)."""
    slots = rec.get("slots")
    ok = isinstance(slots, int) and not isinstance(slots, bool) and MIN_SLOTS <= slots <= MAX_SLOTS
    ident = rec.get("identity") if identity_ok(rec.get("identity")) else None
    argv = rec.get("ssh_argv")
    if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)):
        argv = default_ssh_argv(cfg, identity_tag(ident)) if ident else default_ssh_argv(cfg)
    argv = list(argv)
    if ident:
        argv += ["-i", ident, "-o", "IdentitiesOnly=yes"]
    kinds = rec.get("kinds")
    return {**rec, "slots": slots if ok else MIN_SLOTS,
            "kinds": kinds if isinstance(kinds, list) else list(DEFAULT_KINDS),
            "nw": rec.get("nw") if isinstance(rec.get("nw"), str) and rec["nw"] else "nw",
            "enabled": rec.get("enabled") is not False, "ssh_argv": list(argv),
            "cooldown_until": rec.get("cooldown_until")}


def load(cfg) -> list[dict]:
    """The registered workers in registry order, defaults filled in; `[]` when the file is
    missing, empty or unreadable."""
    try:
        return [normalize(cfg, r) for r in read_strict(cfg)]
    except (OSError, ValueError):
        return []


def update(cfg, fn):
    """Apply `fn(records)` to the raw record list under the registry lock and write the result
    atomically. `fn` mutates the list in place; its return value is
    returned. An exception from `fn`, or an unreadable file (`ValueError`),
    leaves the file untouched."""
    p = path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p.with_name(p.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        records = read_strict(cfg)
        result = fn(records)
        _atomic_write(p, json.dumps({"schema": SCHEMA, "workers": records}, indent=2) + "\n")
        return result
    finally:
        os.close(fd)                            # drops the flock


def _parse_ts(value):
    if not isinstance(value, str):
        return None
    try:
        t = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return t if t.tzinfo else t.astimezone()


def in_cooldown(rec: dict, now=None) -> bool:
    until = _parse_ts(rec.get("cooldown_until"))
    now = now or dt.datetime.now()
    if now.tzinfo is None:
        now = now.astimezone()
    return until is not None and until > now


def gate_pools(cfg, now=None) -> list[tuple]:
    """`[(worker, slots), ...]` for enabled gate workers out of cooldown, in registry order,
    then `[(None, agentic.gates.slots)]`: the `pools` argument of `gate_slots.acquire`."""
    from . import gate_slots
    pools = [(w["name"], w["slots"]) for w in load(cfg)
             if w["enabled"] and "gate" in w["kinds"] and not in_cooldown(w, now)]
    return pools + [(LOCAL, gate_slots.settings(cfg)["slots"])]


def check(cfg, rec: dict) -> dict:
    """Probe a worker: `{name, ok, errors, warnings, info}`. Runs `ssh_argv + [DEST, nw,
    "worker", "info", "--json"]` with a 30 s timeout. Fails on an SSH failure (a timeout
    included), a protocol other than `nw.job.v1` or no `git`; a version or sha different from
    this checkout's is a warning (the gate runs the campaign tree's code, not the worker's).
    A record with an `identity` must also be bound to the forced command (`_forced_probe`);
    when its check fails, the ControlMaster it may have opened is closed (`_close_master`)."""
    w = normalize(cfg, rec)
    out = {"name": w["name"], "ok": False, "errors": [], "warnings": [], "info": None}
    _check(cfg, w, out)
    out["ok"] = not out["errors"]
    if identity_ok(w.get("identity")) and not out["ok"]:
        # whatever key authenticated a failed check (an ssh_config IdentityFile fallback, an
        # unrestricted line), later connections (the next check, gate jobs) must not
        # multiplex onto its master
        _close_master(w)
    return out


def _check(cfg, w: dict, out: dict) -> None:
    from . import worker as W
    try:
        ensure_ssh_dir(cfg, TAG_LEN if identity_ok(w.get("identity")) else 0)
    except OSError as e:
        out["errors"].append(f"ssh socket directory unusable: {e}")
        return
    argv = w["ssh_argv"] + [w["ssh"], w["nw"], "worker", "info", "--json"]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=CHECK_TIMEOUT_S,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        out["errors"].append(f"ssh timed out after {CHECK_TIMEOUT_S:g}s")
        return
    except OSError as e:
        out["errors"].append(f"ssh failed to start: {e}")
        return
    if p.returncode != 0:
        tail = (p.stderr or "").strip().splitlines()[-1:] or [""]
        out["errors"].append(f"ssh failed (exit {p.returncode}): {tail[0]}".rstrip(": "))
        return
    try:
        info = json.loads(p.stdout)
        if not isinstance(info, dict):
            raise ValueError("not a mapping")
    except ValueError as e:
        out["errors"].append(f"`worker info --json` gave unreadable output: {e}")
        return
    out["info"] = info
    if info.get("protocol") != W.JOB_SCHEMA:
        out["errors"].append(f"protocol is {info.get('protocol')!r}, not {W.JOB_SCHEMA!r}")
    if not info.get("git"):
        out["errors"].append("git is missing on the worker")
    mine = W.info(cfg)
    if info.get("version") != mine.get("version"):
        out["warnings"].append(f"version {info.get('version')!r} differs from "
                               f"the coordinator's {mine.get('version')!r}")
    elif info.get("git_sha") and mine.get("git_sha") and info["git_sha"] != mine["git_sha"]:
        out["warnings"].append(f"sha {str(info['git_sha'])[:12]} differs from "
                               f"the coordinator's {str(mine['git_sha'])[:12]}")
    if identity_ok(w.get("identity")):     # even after another error: say which key ran it
        err = _forced_probe(w)
        if err:
            out["errors"].append(err)


FORCED_PROBE = "echo nw-forced-command-probe"


def _forced_probe(w: dict) -> str | None:
    """Send a command the SPEC-0336 forced command must refuse; an error string unless it is
    refused. `IdentitiesOnly` still lets ssh fall back to `IdentityFile` entries of the
    human's ssh_config, so a working `info` alone does not prove the dedicated key and its
    forced line are what authenticated."""
    try:
        p = subprocess.run(w["ssh_argv"] + [w["ssh"], FORCED_PROBE], capture_output=True,
                           text=True, timeout=CHECK_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return f"forced-command probe: ssh timed out after {CHECK_TIMEOUT_S:g}s"
    except OSError as e:
        return f"forced-command probe: ssh failed to start: {e}"
    if p.returncode != 0 and "refused:" in (p.stderr or ""):
        return None
    if p.returncode == 0:
        return (f"{w['ssh']} ran an unrestricted command: the dedicated key "
                f"{w['identity']} is not bound to the forced command (check its "
                "authorized_keys line, and IdentityFile entries for this host in ssh_config)")
    tail = (p.stderr or "").strip().splitlines()[-1:] or [""]
    return f"forced-command probe failed (exit {p.returncode}): {tail[0]}".rstrip(": ")


def _close_master(w: dict) -> None:
    """`ssh -O exit` the record's ControlMaster, if any; best-effort."""
    try:
        subprocess.run(w["ssh_argv"] + ["-O", "exit", w["ssh"]], capture_output=True,
                       timeout=CHECK_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError):
        pass
