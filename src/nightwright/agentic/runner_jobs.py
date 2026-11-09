"""Runner jobs (SPEC-0487): CLI persona runs executed by the runner, which holds the credentials.

The runner (SPEC-0483) is the only process that resolves `agentic.credentials` into its
environment; the CLI never reads `pass`. So while a runner holds the lease on this host,
`nw spec review` drops a request in `<data>/agentic/runner-jobs/<id>/request.json` and waits.
The runner claims it (an atomic rename to `claimed.json`) and starts a detached child
`python -m nightwright queue runner-job <dir>` with its own environment; the child runs the
ordinary review and writes `result.json`. Claim and cancel are renames of the same file, so
exactly one wins. Job files carry names and paths only, never a credential value."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..export import _atomic_write
from . import lease as LS
from .store import agentic_dir, now_iso

SCHEMA = "nw.runner.job.v1"
JOB_ENV = "NW_RUNNER_JOB"
KINDS = ("spec_review",)
CLAIM_TIMEOUT_S = 300.0          # a long tick (a close on the main thread) delays the claim
DEAD_GRACE_S = 2.0
KEEP_S = 7 * 86400
_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")
_children: list = []             # Popen objects this process started, reaped by `service`
_lock = threading.Lock()


def jobs_dir(cfg) -> Path:
    return agentic_dir(cfg) / "runner-jobs"


def in_job() -> bool:
    return bool(os.environ.get(JOB_ENV))


def _read(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(p: Path, doc: dict) -> None:
    _atomic_write(p, json.dumps(doc, indent=2) + "\n")


def runner_here(cfg) -> dict | None:
    """The lease status when a live (non-stale) runner on this host holds the lease."""
    try:
        st = LS.lease_status(cfg)
    except (OSError, ValueError):
        return None
    if not st.get("held") or st.get("stale"):
        return None
    if st.get("host") and st["host"] != socket.gethostname():
        return None
    return st


def serves_jobs(st: dict | None) -> bool:
    """A runner that publishes `credentials` (even `{}`) runs `service`; an older one never
    claims a job."""
    return bool(st) and st.get("credentials") is not None


def delegate(cfg) -> dict | None:
    """The runner to hand a persona run to, or None (no runner here, a runner that predates
    SPEC-0487, or this process is already a runner job)."""
    if in_job():
        return None
    st = runner_here(cfg)
    return st if serves_jobs(st) else None


# ---- CLI side ------------------------------------------------------------------------------

def submit(cfg, kind: str, fields: dict) -> Path:
    if kind not in KINDS:
        raise ValueError(f"unknown runner job kind {kind!r}")
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    tag = fields.get("spec_id") or "job"
    jid = _ID_RE.sub("-", f"{kind}-{tag}-{stamp}-{os.getpid()}-{secrets.token_hex(2)}")
    d = jobs_dir(cfg) / jid
    d.mkdir(parents=True, exist_ok=False)
    _write(d / "request.json", {"schema": SCHEMA, "id": jid, "kind": kind, **fields,
                                "submitted_at": now_iso(), "submitter_pid": os.getpid()})
    return d


def cancel(job: Path) -> bool:
    """Withdraw an unclaimed request; False when the runner claimed it first."""
    try:
        os.rename(job / "request.json", job / "cancelled.json")
        return True
    except OSError:
        return False


def _alive(pid) -> bool:
    """The pid runs and is not a zombie (an exited child the runner has not reaped yet)."""
    try:
        pid = int(pid)
        os.kill(pid, 0)
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat.rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def wait(cfg, job: Path, *, claim_timeout_s: float = CLAIM_TIMEOUT_S, poll_s: float = 0.5,
         clock=time.monotonic, sleep=time.sleep, note=None) -> dict:
    """Block until `result.json`; return it. Raise ValueError when the runner never picks the
    job up (it is then cancelled) or the child ends without a result."""
    t0 = clock()
    claimed = False
    dead_at = None
    try:
        while True:
            res = _read(job / "result.json")
            if isinstance(res, dict):
                return res
            if not claimed:
                if (job / "claimed.json").exists():
                    claimed = True
                    if note:
                        note("the runner started the job; waiting for the result")
                elif runner_here(cfg) is None or clock() - t0 >= claim_timeout_s:
                    if cancel(job):
                        why = ("the runner stopped" if runner_here(cfg) is None
                               else f"it was not claimed in {claim_timeout_s:g}s")
                        raise ValueError(
                            f"the runner did not pick up the job ({why}); check it with "
                            "`nw doctor` / `nw queue status`, then re-run")
                    claimed = True
            if claimed:
                child = _read(job / "child.json") or {}
                if child.get("pid") and not _alive(child["pid"]):
                    dead_at = dead_at if dead_at is not None else clock()
                    if clock() - dead_at >= DEAD_GRACE_S and not (job / "result.json").exists():
                        raise ValueError(f"the runner's job ended without a result "
                                         f"(see {job / 'job.log'})")
            sleep(poll_s)
    except KeyboardInterrupt:
        if not claimed:
            cancel(job)
        raise


# ---- runner side ---------------------------------------------------------------------------

def _spawn(job: Path, env: dict):
    log = open(job / "job.log", "ab")
    try:
        return subprocess.Popen([sys.executable, "-m", "nightwright", "queue", "runner-job",
                                 str(job)], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                env=env, start_new_session=True, close_fds=True)
    finally:
        log.close()


def _reap() -> None:
    with _lock:
        _children[:] = [p for p in _children if p.poll() is None]


def _prune(root: Path, now: float) -> None:
    for d in root.iterdir():
        res = d / "result.json"
        done = res if res.exists() else (d / "cancelled.json")
        try:
            if done.exists() and now - done.stat().st_mtime > KEEP_S:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def service(cfg, *, spawn=None, stop=None, environ=None) -> list:
    """Reap, prune, and claim + start every pending job. Returns the claimed ids. Never
    raises (the tick must not fail on the inbox)."""
    out: list = []
    try:
        _reap()
        root = jobs_dir(cfg)
        if not root.is_dir():
            return out
        _prune(root, time.time())
        if stop is not None and stop.is_set():
            return out
        spawn = spawn or _spawn
        base = dict(os.environ if environ is None else environ)
        for d in sorted(root.iterdir()):
            req = d / "request.json"
            if not req.is_file():
                continue
            try:
                os.rename(req, d / "claimed.json")
            except OSError:
                continue                                # cancelled or claimed meanwhile
            doc = _read(d / "claimed.json") or {}
            try:
                proc = spawn(d, {**base, JOB_ENV: d.name})
                pid = getattr(proc, "pid", None)
                if isinstance(proc, subprocess.Popen):
                    with _lock:
                        _children.append(proc)
                _write(d / "child.json", {"pid": pid, "started_at": now_iso()})
            except Exception as e:                      # noqa: BLE001
                _write(d / "result.json", {"ok": False,
                                           "error": f"the runner could not start the job: {e}"})
            out.append(d.name)
            try:
                from .queue import journal
                journal(cfg, "runner_job_started", job=d.name, job_kind=doc.get("kind"),
                        spec_id=doc.get("spec_id"))
            except Exception:                           # noqa: BLE001
                pass
    except Exception:                                   # noqa: BLE001
        pass
    return out


# ---- the child -----------------------------------------------------------------------------

def run_job(cfg, job) -> int:
    """`nw queue runner-job DIR`: run the claimed request, always write `result.json`."""
    job = Path(job)
    doc = _read(job / "claimed.json") or {}
    try:
        if doc.get("schema") != SCHEMA or doc.get("kind") not in KINDS:
            raise ValueError(f"{job}: not a claimed {SCHEMA} job")
        from . import spec_review as SR
        review = SR.run_spec_review(cfg, Path(doc["spec_path"]), doc["spec_id"],
                                    profile=doc.get("profile"), origin=doc.get("origin"))
        res = {"ok": True, "review": review}
    except ValueError as e:
        res = {"ok": False, "error": str(e)}
    except Exception as e:                              # noqa: BLE001
        res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    res["finished_at"] = now_iso()
    _write(job / "result.json", res)
    return 0 if res["ok"] else 1
