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
import time
from pathlib import Path

from ..export import _atomic_write
from .store import agentic_dir, now_iso

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


def ensure_record_ssh_dir(cfg, w: dict) -> Path | None:
    """`ensure_ssh_dir` for the ControlPath of a normalized record `w`: an `identity` record's
    socket name is `TAG_LEN` bytes longer, so `default_ssh_argv` may pick another candidate
    for it, and this creates and checks that one (every caller running `w["ssh_argv"]`)."""
    return ensure_ssh_dir(cfg, TAG_LEN if identity_ok(w.get("identity")) else 0)


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
            "cooldown_until": rec.get("cooldown_until"),
            "draining": rec.get("draining") is True}


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


def placeable(rec: dict) -> bool:
    """SPEC-0343: a new gate may be placed on the normalized record `rec`: enabled, not
    draining, and taking gates."""
    return bool(rec["enabled"]) and not rec.get("draining") and "gate" in rec["kinds"]


def placeable_by_name(cfg, name) -> bool:
    """`placeable` for the worker `name`, from a fresh read of the registry; False for an
    unknown name (or an unreadable registry)."""
    rec = next((w for w in load(cfg) if w["name"] == name), None)
    return rec is not None and placeable(rec)


def local_overflow(cfg) -> bool:
    """SPEC-0374: `agentic.gates.local_overflow` (default false). Only a literal `true` turns
    it on: a gate may then overflow to a free local slot while every worker slot is busy."""
    raw = (((cfg.get("agentic") or {}).get("gates")) or {}).get("local_overflow")
    return raw is True


def gate_pools(cfg, now=None, *, exclude=()) -> list[tuple]:
    """The `pools` argument of `gate_slots.acquire`: `[(worker, slots), ...]` for placeable
    gate workers out of cooldown (not in `exclude`), in registry order. SPEC-0374: while that
    list is non-empty it is the whole answer, so a gate waits for a worker slot instead of
    taking a local one; the local pool `(None, agentic.gates.slots)` is the only pool when no
    worker is placeable, and is appended last only with `agentic.gates.local_overflow: true`."""
    from . import gate_slots
    pools = [(w["name"], w["slots"]) for w in load(cfg)
             if placeable(w) and not in_cooldown(w, now) and w["name"] not in set(exclude)]
    if pools and not local_overflow(cfg):
        return pools
    return pools + [(LOCAL, gate_slots.settings(cfg)["slots"])]


def draining_jobs(cfg) -> dict:
    """SPEC-0343: `{worker: k}`, `k` the number of its non-final remote job records."""
    from . import remote_jobs as RJ
    out: dict = {}
    for rec in RJ.live(cfg):
        if isinstance(w := rec.get("worker"), str):
            out[w] = out.get(w, 0) + 1
    return out


UPDATE_DRAIN_TIMEOUT_H = 6.0


def _workers_cfg(cfg) -> dict:
    w = ((cfg.get("agentic") or {}).get("workers"))
    return w if isinstance(w, dict) else {}


def update_drain_timeout_h(cfg) -> float:
    """SPEC-0386: `agentic.workers.update_drain_timeout_h` (default 6): how long a worker
    scheduled for an update may stay draining before the queue gives up and returns it to
    service. A non-positive or non-number value falls back to the default."""
    raw = _workers_cfg(cfg).get("update_drain_timeout_h")
    ok = isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0
    return float(raw) if ok else UPDATE_DRAIN_TIMEOUT_H


def auto_update(cfg) -> bool:
    """SPEC-0386: `agentic.workers.auto_update` (default false); only a literal `true`."""
    return _workers_cfg(cfg).get("auto_update") is True


def update_pending(rec: dict) -> dict | None:
    """The `update_pending` marker of a record that is draining for an update, else None.
    The marker counts only together with `draining: true` (`on` / `off` clear both)."""
    m = rec.get("update_pending")
    return m if isinstance(m, dict) and rec.get("draining") is True else None


def mark_update(rec: dict, since: str) -> None:
    """SPEC-0386: schedule an update on the raw record `rec` (inside a registry `update`)."""
    rec["draining"] = True
    rec["update_pending"] = {"reason": "update", "since": since}


def _drained(cfg, name: str) -> bool:
    """A draining worker is idle: no non-final job record names it and no slot of its pool is
    held (a gate between `acquire` and `submit` has a slot but no record yet)."""
    from . import gate_slots
    from . import remote_jobs as RJ
    return not any(r.get("worker") == name for r in RJ.live(cfg)) \
        and not gate_slots.held(cfg, name)


class _NoDrain(Exception):
    """Raised inside `finish_drains`' update when the locked re-check finds nothing: no write."""


def _check_job_records(cfg) -> None:
    """Raise `ValueError` (`OSError` for an unreadable dir) when `remote-jobs/` holds a record
    file `remote_jobs.read` cannot use (unreadable, unparseable, another `schema`), a record
    whose `state` is missing or outside `remote_jobs.STATES`, or a non-final record without a
    string `worker`: its job may still run on a draining worker,
    and `live` / `_drained` would silently pass it over. Only regular, non-hidden `*.json`
    files are record files (job ids never start with a dot): a directory or a dotfile is
    not a record."""
    from . import remote_jobs as RJ
    d = RJ.jobs_dir(cfg)
    if not d.exists():
        return
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json") or name.startswith("."):
            continue
        p = d / name
        if not p.is_file() and not p.is_symlink():
            continue                                        # a directory (or pruned file)
        rec = RJ.read(cfg, p.stem)
        if rec is None:
            if p.exists() or p.is_symlink():                # a pruned file is no record
                raise ValueError(f"unreadable job record {p}")
            continue
        if rec.get("state") not in RJ.STATES:               # missing, null, or unknown: it
            raise ValueError(f"job record {p} has no known state")  # may be a running job
        if rec.get("state") in RJ.LIVE and not isinstance(rec.get("worker"), str):
            raise ValueError(f"live job record {p} names no worker")


def finish_drains(cfg) -> list[str]:
    """SPEC-0343, every queue tick: each draining worker that is idle (`_drained`) gets
    `enabled: false` and loses `draining`, in one `update`; returns their names. The check runs
    again inside the registry lock, so a concurrent `on` / `off` / `drain` cannot interleave;
    the registry is written only when that locked re-check disables something. Raises `OSError`
    / `ValueError` (nothing disabled) when the registry, the `remote-jobs` dir or any record in
    it is unreadable."""
    names = [r["name"] for r in read_strict(cfg)
             if r.get("draining") is True and not update_pending(r)]
    if not names:
        return []
    _check_job_records(cfg)
    if not any(_drained(cfg, n) for n in names):
        return []

    def fn(records):
        _check_job_records(cfg)                 # again under the lock: a record that turned
        done = []                               # unusable since the pre-check aborts the write
        for r in records:
            if r.get("draining") is True and not update_pending(r) \
                    and _drained(cfg, r["name"]):
                r["enabled"] = False
                r.pop("draining", None)
                done.append(r["name"])
        if not done:
            raise _NoDrain()                    # nothing changed: leave the file alone
        return done
    try:
        return update(cfg, fn)
    except _NoDrain:
        return []


def due_updates(cfg, now=None) -> tuple[list[dict], list[dict]]:
    """SPEC-0386, every queue tick: `(idle, timed_out)` normalized records of the enabled
    workers draining for an update. `idle`: no live job and no held slot (`_drained`).
    `timed_out`: not idle and `since` older than `update_drain_timeout_h`. A marker with an
    unreadable `since` counts from now (never times out). Raises like `finish_drains`."""
    pend = [r for r in read_strict(cfg) if r.get("enabled") is not False and update_pending(r)]
    if not pend:
        return [], []
    _check_job_records(cfg)
    now = now or dt.datetime.now()
    if now.tzinfo is None:
        now = now.astimezone()
    limit = dt.timedelta(hours=update_drain_timeout_h(cfg))
    idle, late = [], []
    for r in pend:
        if _drained(cfg, r["name"]):
            idle.append(normalize(cfg, r))
            continue
        since = _parse_ts(update_pending(r).get("since"))
        if since is not None and now - since > limit:
            late.append(normalize(cfg, r))
    return idle, late


def end_update(cfg, name: str, since, state: str, detail: str = "") -> bool:
    """SPEC-0386: return `name` to service after its scheduled update ended (`state`: updated,
    unchanged, failed, old, timeout): drop `draining` and `update_pending` (the worker stays
    enabled with its slots and kinds) and record `update_last`. Only if the record still
    carries the marker with this `since` (an `on` / `off` / newer schedule in between wins).
    Returns whether it wrote."""
    done = []

    def fn(records):
        r = next((r for r in records if r.get("name") == name), None)
        m = r.get("update_pending") if r else None
        if not isinstance(m, dict) or m.get("since") != since:
            raise _NoDrain()
        r.pop("draining", None)
        r.pop("update_pending", None)
        r["update_last"] = {"state": state, "at": dt.datetime.now().astimezone().isoformat(
            timespec="seconds"), "detail": detail[:300]}
        done.append(1)
    try:
        update(cfg, fn)
    except _NoDrain:
        return False
    return bool(done)


def check(cfg, rec: dict) -> dict:
    """Probe a worker: `{name, ok, errors, warnings, info}`. Runs `ssh_argv + [DEST, nw,
    "worker", "info", "--json"]` with a 30 s timeout. Fails on an SSH failure (a timeout
    included), a protocol other than `nw.job.v1` or no `git`; a version or build id
    different from this checkout's is a warning (the gate runs the campaign tree's code, not
    the worker's).
    A record with an `identity` must also be bound to the forced command (`_forced_probe`);
    when the probe proves it is not (an unrestricted command ran), the ControlMaster that
    session rode is closed (`_close_master`). Any other failure leaves the master alone:
    gate jobs multiplex on it, and `-O exit` would end every one in flight."""
    w = normalize(cfg, rec)
    out = {"name": w["name"], "ok": False, "errors": [], "warnings": [], "info": None}
    if _check(cfg, w, out):
        # the key that authenticated (an ssh_config IdentityFile fallback, an unrestricted
        # line) is not the forced one: later connections must not multiplex onto its master
        _close_master(w)
    out["ok"] = not out["errors"]
    if isinstance(out["info"], dict):
        _cache_info(cfg, w["name"], out["info"])
    return out


def _cache_info(cfg, name: str, info: dict) -> None:
    """SPEC-0341: keep the worker's last `info` on its registry record; the coordinator reads
    its `features` from there (no cached `info` reads as a worker without `async`)."""
    def fn(records):
        for r in records:
            if r.get("name") == name:
                r["info"] = info
    try:
        update(cfg, fn)
    except (OSError, ValueError):
        pass                                    # an unreadable registry is reported elsewhere


def _check(cfg, w: dict, out: dict) -> bool:
    """Fill `out`; True only when the forced-command probe ran an unrestricted command."""
    from . import worker as W
    try:
        ensure_record_ssh_dir(cfg, w)
    except OSError as e:
        out["errors"].append(f"ssh socket directory unusable: {e}")
        return False
    argv = w["ssh_argv"] + [w["ssh"], w["nw"], "worker", "info", "--json"]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=CHECK_TIMEOUT_S,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        out["errors"].append(f"ssh timed out after {CHECK_TIMEOUT_S:g}s")
        return False
    except OSError as e:
        out["errors"].append(f"ssh failed to start: {e}")
        return False
    if p.returncode != 0:
        tail = (p.stderr or "").strip().splitlines()[-1:] or [""]
        out["errors"].append(f"ssh failed (exit {p.returncode}): {tail[0]}".rstrip(": "))
        return False
    try:
        info = json.loads(p.stdout)
        if not isinstance(info, dict):
            raise ValueError("not a mapping")
    except ValueError as e:
        out["errors"].append(f"`worker info --json` gave unreadable output: {e}")
        return False
    out["info"] = info
    if info.get("protocol") != W.JOB_SCHEMA:
        out["errors"].append(f"protocol is {info.get('protocol')!r}, not {W.JOB_SCHEMA!r}")
    if not info.get("git"):
        out["errors"].append("git is missing on the worker")
    mine = W.info(cfg)
    if info.get("version") != mine.get("version"):
        out["warnings"].append(f"version {info.get('version')!r} differs from "
                               f"the coordinator's {mine.get('version')!r}")
    if mine.get("build_id") and info.get("build_id") != mine["build_id"]:
        out["warnings"].append(
            f"worker stale: build {str(info.get('build_id') or 'none')[:12]} differs from "
            f"the coordinator's {mine['build_id'][:12]}; run nw workers bootstrap "
            f"{w['name']} --ssh {w['ssh']} --reinstall")
    feats = info.get("features")
    if not (isinstance(feats, list) and "async" in feats):     # SPEC-0341: still gate-job
        out["warnings"].append("info lacks the `async` feature: gates run through the "
                               "synchronous `gate-job`, without re-attach or adoption; "
                               "upgrade nightwright on the worker")
    if not identity_ok(w.get("identity")):
        return False
    err, unbound = _forced_probe(w)        # even after another error: say which key ran it
    if err:
        out["errors"].append(err)
    return unbound


FORCED_PROBE = "echo nw-forced-command-probe"


def _forced_probe(w: dict) -> tuple[str | None, bool]:
    """Send a command the SPEC-0336 forced command must refuse: `(error, unbound)`. `error`
    is None when it is refused; `unbound` is True only when the command actually ran (exit
    0), the one outcome that proves the master is bad. `IdentitiesOnly` still lets ssh fall
    back to `IdentityFile` entries of the human's ssh_config, so a working `info` alone does
    not prove the dedicated key and its forced line are what authenticated."""
    try:
        p = subprocess.run(w["ssh_argv"] + [w["ssh"], FORCED_PROBE], capture_output=True,
                           text=True, timeout=CHECK_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return f"forced-command probe: ssh timed out after {CHECK_TIMEOUT_S:g}s", False
    except OSError as e:
        return f"forced-command probe: ssh failed to start: {e}", False
    if p.returncode != 0 and "refused:" in (p.stderr or ""):
        return None, False
    if p.returncode == 0:
        return (f"{w['ssh']} ran an unrestricted command: the dedicated key "
                f"{w['identity']} is not bound to the forced command (check its "
                "authorized_keys line, and IdentityFile entries for this host in ssh_config)",
                True)
    tail = (p.stderr or "").strip().splitlines()[-1:] or [""]
    return f"forced-command probe failed (exit {p.returncode}): {tail[0]}".rstrip(": "), False


def _close_master(w: dict) -> None:
    """`ssh -O exit` the record's ControlMaster, if any; best-effort."""
    try:
        subprocess.run(w["ssh_argv"] + ["-O", "exit", w["ssh"]], capture_output=True,
                       timeout=CHECK_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError):
        pass


# ---- lost-job window and the worker escalation (SPEC-0342) ------------------------------------

LOSSES_SCHEMA = 1
LOSS_WINDOW_S = 3600.0              # three losses in this sliding window raise one escalation
LOSS_THRESHOLD = 3
ESCALATION_KIND = "worker_lost"
_now = time.time                    # the loss window's clock (tests swap it)


def losses_path(cfg) -> Path:
    return agentic_dir(cfg) / "worker-losses.json"


def _read_losses(p: Path) -> dict:
    """The losses document; a missing or unreadable file starts empty (it is a counter, and
    the next write replaces it)."""
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = None
    if not isinstance(doc, dict) or not isinstance(doc.get("workers"), dict):
        doc = {"schema": LOSSES_SCHEMA, "workers": {}}
    return doc


def read_losses(cfg) -> dict:
    return _read_losses(losses_path(cfg))


def _epoch(ts) -> float | None:
    t = _parse_ts(ts)
    return t.timestamp() if t is not None else None


def _iso(epoch_s: float) -> str:
    return dt.datetime.fromtimestamp(epoch_s).astimezone().isoformat(timespec="seconds")


def _worker_fact(name: str) -> str:
    return f"worker={name}"


def _open_escalation_for(cfg, name: str) -> dict | None:
    from . import escalations as ESC
    return next((r for r in ESC.list_all(cfg, status="open")
                 if r.get("kind") == ESCALATION_KIND
                 and _worker_fact(name) in (r.get("facts") or [])), None)


def _raise_escalation(cfg, name: str, recent: list, n: int) -> dict:
    from . import escalations as ESC
    facts = [_worker_fact(name)] + [f"{x.get('job_id')}: {x.get('reason')}" for x in recent]
    plan = ESC.plan_escalate(cfg, None, question=f"Worker {name} lost {n} jobs in the last "
                                                 f"hour. Disable it?",
                             options=["off", "keep"], recommend=1, facts=facts,
                             frm="conductor", kind=ESCALATION_KIND)
    return ESC.apply_escalate(cfg, plan)


def _withdraw_unraised(cfg, esc_id: str, why: str) -> None:
    """A worker escalation whose mail to `human` failed is withdrawn, so the open record does
    not block the next loss's attempt. A failed withdraw leaves it open (it still blocks)."""
    from . import escalations as ESC
    try:
        ESC.withdraw(cfg, esc_id, f"not raised: {why}"[:300], "conductor", clear_request=False)
    except (OSError, ValueError):
        pass


def record_loss(cfg, name: str, *, job_id: str, reason: str, now: float | None = None) -> dict:
    """Record one `worker_job_lost` of worker `name` in `worker-losses.json`, under its lock,
    and raise the campaign-less worker escalation when three or more losses remain inside
    the sliding `LOSS_WINDOW_S`, the last one is older than the window (or there is none) and
    no worker escalation for `name` is open. Returns `{"losses": n, "escalation": id | None,
    "error": text | None}`; a failure to raise is `error` (the next loss tries again), never
    an exception. A failed mail to `human` is a failure to raise (the record is withdrawn); a
    failed event or notifier stays on the record's `notified`, as for any escalation, and the
    notifier is retried by `remind_due`. A file I/O failure raises `OSError`."""
    now = _now() if now is None else now
    p = losses_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p.with_name(p.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        doc = _read_losses(p)
        w = doc["workers"].get(name)
        if not isinstance(w, dict):
            w = {}
        losses = [t for t in w.get("losses") or [] if isinstance(t, str)]
        recent = [r for r in w.get("recent") or [] if isinstance(r, dict)]
        losses.append(_iso(now))
        recent = (recent + [{"at": _iso(now), "job_id": job_id, "reason": reason}])[-3:]
        losses = [t for t in losses if (e := _epoch(t)) is not None and now - e <= LOSS_WINDOW_S]
        w = {"losses": losses, "recent": recent,
             "escalated_at": w.get("escalated_at"), "escalation": w.get("escalation")}
        out = {"losses": len(losses), "escalation": None, "error": None}
        last = _epoch(w["escalated_at"])
        if len(losses) >= LOSS_THRESHOLD and (last is None or now - last > LOSS_WINDOW_S):
            try:
                if _open_escalation_for(cfg, name) is None:
                    rec = _raise_escalation(cfg, name, recent, len(losses))
                    if rec.get("mail_id") is None:  # the human was never asked: not raised
                        why = (rec.get("notified") or {}).get("error") or "mail not sent"
                        _withdraw_unraised(cfg, rec["id"], why)
                        out["error"] = f"worker {name} escalation not raised: {why}"
                    else:
                        w["escalated_at"], w["escalation"] = _iso(now), rec["id"]
                        out["escalation"] = rec["id"]
            except (OSError, ValueError) as e:
                out["error"] = f"worker {name} escalation not raised: {e}"
        doc["workers"][name] = w
        doc["schema"] = LOSSES_SCHEMA
        _atomic_write(p, json.dumps(doc, indent=2, sort_keys=True) + "\n")
        return out
    finally:
        os.close(fd)


def _global_note(cfg, text: str) -> None:
    from . import events as E
    try:
        E.append(cfg, "note", by="queue", data={"text": text, "author": "queue"})
    except (OSError, ValueError):
        pass


def apply_answered_escalations(cfg) -> list[tuple[str, str]]:
    """Once per queue tick: carry out answered worker escalations not yet applied. Option 1
    `off` sets the worker's `enabled: false` (the `nw workers off` path); option 2 `keep`
    writes nothing. An unregistered worker or a free-text answer changes nothing and appends
    a queue-wide `note`. Each record then gets `applied_at` and `applied` (`off`, `keep`,
    `skipped`), so it is applied once. Returns `[(escalation id, applied), ...]`."""
    from . import escalations as ESC
    out = []
    for rec in ESC.list_all(cfg, status="answered"):
        if rec.get("kind") != ESCALATION_KIND or rec.get("applied_at"):
            continue
        facts = [f for f in rec.get("facts") or [] if isinstance(f, str)]
        name = next((f[len("worker="):] for f in facts if f.startswith("worker=")), None)
        answer = rec.get("answer") or {}
        choice = answer.get("choice") if answer.get("kind") == "choose" else None
        applied, why = "skipped", None
        try:
            names = {r["name"] for r in read_strict(cfg)}
        except (OSError, ValueError):
            continue                            # an unreadable registry: try again next tick
        if not name or name not in names:
            why = f"worker {name} is no longer registered"
        elif choice == 1:
            def fn(records):
                r = next((r for r in records if r.get("name") == name), None)
                if r is not None:
                    r["enabled"] = False
                return r is not None
            try:
                found = update(cfg, fn)
            except (OSError, ValueError):
                continue
            applied = "off" if found else "skipped"
            why = None if found else f"worker {name} is no longer registered"
        elif choice == 2:
            applied = "keep"
        else:
            why = "the answer is not one of its options"
        if why:
            _global_note(cfg, f"{rec['id']}: {why}; nothing applied")
        try:
            ESC._mutate(cfg, rec["id"], lambda r: r.update(applied_at=now_iso(),
                                                            applied=applied))
        except (OSError, ValueError):
            continue
        out.append((rec["id"], applied))
    return out
