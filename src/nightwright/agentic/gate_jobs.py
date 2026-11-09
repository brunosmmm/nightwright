"""Standalone gate jobs (SPEC-0381): `nw gate run` gates a worktree or a branch on a worker box
without a campaign. A job is a campaign-shaped state (`{id: GATEJOB-NNNN, project, repo}`, as the
SPEC-0379 sentinel builds one) run by `remote_gates.run_detached` under `gate_slots.acquire` on
worker pools only, never locally. Campaigns win slot contention (a `skip` predicate while any
non-job gate waits); `agentic.gate_jobs` caps jobs per requester and box-wide with flock tokens
taken before the slot. Records live in `<agentic>/gatejobs/<id>/job.json`, logs in `gates/`."""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from ..export import _atomic_write
from . import events as E
from . import gate_slots as GS
from . import gates as G
from . import project as P
from . import remote_gates as RG
from . import workers as WK
from .store import agentic_dir, now_iso

SCHEMA = "nw.gatejob.v1"
RECORD_SCHEMA = "nw.gatejob.record.v1"
PREFIX = "GATEJOB-"
ID_RE = re.compile(r"GATEJOB-\d{4,}\Z")
FINAL = ("passed", "failed", "unavailable", "transport_error", "cancelled", "error")
EXIT = {"passed": 0, "failed": 1, "error": 2, "unavailable": 3, "transport_error": 4,
        "lost": 4, "cancelled": 130}
TIERS = ("loop", "close", "all")
DEFAULTS = {"enabled": True, "max_per_requester": 2, "max_slots": None, "retention_days": 7}
DEFAULT_WAIT_S = 300.0
POLL_S = 1.0
SEQ = 1                                 # one gate run per job: logs are `gates/001-<gate>.log`


class JobError(Exception):
    """A run that cannot start: `code` is the exit code (2 usage/precondition, 3 unavailable)."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


# ---- config, paths, records ------------------------------------------------------------------

def _pos_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def settings(cfg) -> dict:
    """`DEFAULTS` overlaid with `agentic.gate_jobs`; a bad value falls back to its default.
    `max_slots` None means half the placeable worker gate slots (at least 1)."""
    raw = ((cfg.get("agentic") or {}).get("gate_jobs")) or {}
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULTS)
    if isinstance(raw.get("enabled"), bool):
        out["enabled"] = raw["enabled"]
    for k in ("max_per_requester", "max_slots"):
        if _pos_int(raw.get(k)):
            out[k] = raw[k]
    r = raw.get("retention_days")
    if isinstance(r, (int, float)) and not isinstance(r, bool) and math.isfinite(r) and r > 0:
        out["retention_days"] = r
    return out


def max_slots(cfg) -> int:
    s = settings(cfg)["max_slots"]
    if s is not None:
        return s
    total = sum(w["slots"] for w in WK.load(cfg) if WK.placeable(w))
    return max(1, total // 2)


def worker_pools(cfg) -> list:
    """`workers.gate_pools` without the local pool: a standalone job never runs locally."""
    return [(name, n) for name, n in WK.gate_pools(cfg) if name is not WK.LOCAL]


def requester(by=None) -> str:
    return str(by or os.environ.get("NW_MAILBOX_ADDR") or os.environ.get("USER") or "cli")


def root(cfg) -> Path:
    return agentic_dir(cfg) / "gatejobs"


def job_dir(cfg, job_id: str) -> Path:
    if not isinstance(job_id, str) or not ID_RE.match(job_id):
        raise ValueError(f"not a gate job id: {job_id!r} (GATEJOB-NNNN)")
    return root(cfg) / job_id


@contextmanager
def _lock(cfg):
    root(cfg).mkdir(parents=True, exist_ok=True)
    fd = os.open(root(cfg) / ".lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def read(cfg, job_id: str) -> dict | None:
    try:
        rec = json.loads((job_dir(cfg, job_id) / "job.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def _write(cfg, rec: dict) -> None:
    _atomic_write(job_dir(cfg, rec["id"]) / "job.json", json.dumps(rec, indent=1) + "\n")


def update(cfg, job_id: str, **fields) -> dict | None:
    """Merge `fields` into the record under the jobs lock; the updated record."""
    with _lock(cfg):
        rec = read(cfg, job_id)
        if rec is None:
            return None
        rec.update(fields)
        _write(cfg, rec)
        return rec


def status_of(rec: dict) -> str:
    """The record's status; a non-final one whose process is gone reads `lost`."""
    st = rec.get("status")
    if st in FINAL:
        return st
    return st if GS._pid_alive(rec.get("pid")) else "lost"


def list_jobs(cfg) -> list[dict]:
    d = root(cfg)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.iterdir()):
        if p.is_dir() and ID_RE.match(p.name):
            rec = read(cfg, p.name)
            if rec is not None:
                out.append(rec)
    return out


def _epoch(iso) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(iso)).timestamp()
    except (TypeError, ValueError):
        return None


def gc(cfg, now: float | None = None) -> list[str]:
    """Delete job dirs that are final (or lost) and older than `retention_days`; their ids."""
    now = time.time() if now is None else now
    keep_s = settings(cfg)["retention_days"] * 86400
    gone = []
    for rec in list_jobs(cfg):
        if status_of(rec) not in FINAL + ("lost",):
            continue
        t = _epoch(rec.get("finished_at")) or _epoch(rec.get("created_at"))
        if t is not None and now - t > keep_s:
            shutil.rmtree(job_dir(cfg, rec["id"]), ignore_errors=True)
            gone.append(rec["id"])
    return gone


# ---- the target: commit, base, gates ---------------------------------------------------------

def _git(cwd, *args) -> str | None:
    p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else None


def resolve(cfg, *, project=None, worktree=None, branch=None, base=None, tier="loop",
            cmds=(), gate_timeout_s: int = 1200) -> dict:
    """What a job gates: `{project, repo, worktree, branch, commit, tree, base, tier, gates,
    skipped, key}`. A precondition that fails raises JobError(2)."""
    if tier not in TIERS:
        raise JobError(2, f"--tier must be one of {', '.join(TIERS)}")
    wt = Path(worktree or os.getcwd()).resolve()
    top = _git(wt, "rev-parse", "--show-toplevel")
    if top is None:
        raise JobError(2, f"{wt} is not inside a git repository")
    wt = Path(top)
    name = project or P.find_project_for_cwd(cfg, str(wt))
    pcfg = {}
    if name:
        try:
            pcfg = P.project_config(cfg, name)
        except ValueError as e:
            if not cmds:
                raise JobError(2, f"{e}; pass --cmd to run ad-hoc gates")
    elif not cmds:
        raise JobError(2, "no registered project with .nw/project.yaml gates here; pass "
                          "--project or --cmd")
    if cmds:
        gates = [{"name": f"cmd-{i}", "cmd": c, "timeout_s": int(gate_timeout_s),
                  "results": {"format": "pytest"}}
                 for i, c in enumerate(cmds, 1)]
        skipped = []
    else:
        if pcfg.get("remote_gates") is False:
            raise JobError(2, f"project {name} sets remote_gates: false; standalone gate jobs "
                              f"run only on worker boxes")
        every = P.gates_for(pcfg, None if tier == "all" else tier)
        skipped = [g["name"] for g in every if g.get("remote") is False]
        gates = [g for g in every if g.get("remote") is not False]
        if not gates:
            raise JobError(2, f"project {name} has no {tier} gates that can run remotely")
    base = base or (pcfg.get("merge") or {}).get("base") or pcfg.get("base") or "main"
    if branch:
        repo = Path(pcfg.get("_root") or wt)
        commit = _git(repo, "rev-parse", "--verify", f"{branch}^{{commit}}")
        if commit is None:
            raise JobError(2, f"cannot resolve branch {branch!r} in {repo}")
        label, worktree_s = branch, None
    else:
        repo = wt
        why = RG.ineligible({}, wt)
        if why is not None:
            raise JobError(2, f"{why['title'].split(';')[0]}: {', '.join(why['files']) or why['detail']}"
                              f" -- the bundle carries commits only; `git add` them first")
        try:
            commit = RG.commit_under_test(wt)
        except RG.RemoteFailure as e:
            raise JobError(2, f"cannot cut the commit under test: {e.error}")
        label, worktree_s = _git(wt, "rev-parse", "--abbrev-ref", "HEAD") or "HEAD", str(wt)
    if _git(repo, "rev-parse", "--verify", f"{base}^{{commit}}") is None:
        raise JobError(2, f"cannot resolve base {base!r} in {repo}")
    tree = _git(repo, "rev-parse", f"{commit}^{{tree}}")
    pcfg_job = {"project": name, "gates": gates}
    state = _state(f"{PREFIX}0000", name, repo, base, commit)
    refusal = RG.header_refusal(pcfg_job, state, cfg, SEQ)
    if refusal is not None:
        raise JobError(2, f"the worker would refuse this job: {refusal}")
    key = hashlib.sha256(json.dumps([os.path.realpath(repo), name, tree, base, gates],
                                    sort_keys=True, default=str).encode()).hexdigest()[:16]
    return {"project": name, "repo": str(repo), "worktree": worktree_s, "branch": label,
            "commit": commit, "tree": tree, "base": base, "tier": tier, "gates": gates,
            "skipped": skipped, "key": key}


def _state(job_id, project, repo, base, commit) -> dict:
    return {"id": job_id, "project": project, "spec_id": None,
            "repo": {"worktree": str(repo), "base": base, "commit": commit}}


# ---- create (idempotent) ---------------------------------------------------------------------

def _next_id(cfg) -> str:
    path = root(cfg) / "counter"
    try:
        n = int(path.read_text().strip())
    except (OSError, ValueError):
        n = 0
    existing = [int(p.name[len(PREFIX):]) for p in root(cfg).glob(f"{PREFIX}*")
                if ID_RE.match(p.name)]
    n = max([n, *existing]) + 1
    _atomic_write(path, f"{n}\n")
    return f"{PREFIX}{n:04d}"


def create(cfg, target: dict, by: str, pid: int | None = None) -> tuple[dict, bool]:
    """`(record, reused)`: a live non-final job with the same key, else a new `queued` one."""
    with _lock(cfg):
        for rec in list_jobs(cfg):
            if rec.get("key") == target["key"] and status_of(rec) not in FINAL + ("lost",):
                return rec, True
        job_id = _next_id(cfg)
        rec = {"schema": RECORD_SCHEMA, "id": job_id, "status": "queued", "exit": None,
               "pid": os.getpid() if pid is None else pid, "requester": by,
               "created_at": now_iso(), "created_epoch": time.time(),
               **{k: target[k] for k in ("key", "project", "repo", "worktree", "branch",
                                         "commit", "tree", "base", "tier", "skipped")},
               "gates": [g["name"] for g in target["gates"]], "gate_cfg": target["gates"],
               "worker": None, "result": [], "failing": [], "changed": None, "error": None}
        (job_dir(cfg, job_id) / "gates").mkdir(parents=True, exist_ok=True)
        _write(cfg, rec)
        return rec, False


# ---- placement: tokens, campaign priority, the slot ------------------------------------------

def _take_token(d: Path, n: int):
    d.mkdir(parents=True, exist_ok=True)
    for k in range(n):
        fd = os.open(d / f"slot-{k}.lock", os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            continue
        return fd
    return None


def _wait_token(d: Path, n: int, wait_ev: threading.Event, poll_s: float):
    while True:
        fd = _take_token(d, n)
        if fd is not None or wait_ev.is_set():
            return fd
        wait_ev.wait(poll_s)


def requester_tokens(cfg, by: str) -> Path:
    return root(cfg) / "tokens" / f"req-{hashlib.sha256(by.encode()).hexdigest()[:12]}"


def share_tokens(cfg) -> Path:
    return root(cfg) / "tokens" / "share"


def campaign_waiting(cfg) -> bool:
    """A live waiter marker that is not a standalone job's (a campaign, the sentinel)."""
    d = GS.slots_dir(cfg)
    if not d.is_dir():
        return False
    for p in d.glob("waiting-*.json"):
        rec = GS._read_json(p)
        if rec and not str(rec.get("campaign") or "").startswith(PREFIX) \
                and GS._pid_alive(rec.get("pid")):
            return True
    return False


def _deadline_watch(stop: threading.Event, wait_ev: threading.Event, deadline: float,
                    done: threading.Event) -> None:
    while not done.is_set() and not wait_ev.is_set():
        if stop.is_set() or time.monotonic() >= deadline:
            wait_ev.set()
            return
        done.wait(0.05)


# ---- execute -------------------------------------------------------------------------------

def _rows(run, jdir: Path) -> list[dict]:
    return [{"name": r.name, "ok": r.ok, "required": r.required, "exit_code": r.exit_code,
             "duration_s": r.duration_s, "log": str(jdir / r.log) if r.log else None,
             "summary": r.summary} for r in run.results]


def _failing(results) -> list[str]:
    ids = []
    for r in results:
        if r.required and not r.ok:
            ids += list((r.summary or {}).get("failing") or [])
    return ids or [r.name for r in results if r.required and not r.ok]


def _event(cfg, rec: dict) -> None:
    try:
        E.append(cfg, "gate_job", project=rec.get("project"), by=rec.get("requester") or "nw",
                 data={"job_id": rec["id"], "status": rec["status"], "exit": rec.get("exit"),
                       "by": rec.get("requester"), "project": rec.get("project"),
                       "branch": rec.get("branch"), "worker": rec.get("worker")})
    except (OSError, ValueError):
        pass                                    # informational: never fail the job over it


def _finish(cfg, job_id: str, status: str, **fields) -> dict:
    rec = read(cfg, job_id) or {}
    dur = round(time.time() - float(rec.get("created_epoch") or time.time()), 1)
    rec = update(cfg, job_id, status=status, exit=EXIT[status], finished_at=now_iso(),
                 duration=dur, **fields)
    _event(cfg, rec)
    return rec


def execute(cfg, job_id: str, *, stop: threading.Event | None = None,
            wait_s: float = DEFAULT_WAIT_S, poll_s: float | None = None) -> dict:
    """Run the recorded job to a final status in this process; the final record. Waits for a
    requester token, a share token and a worker slot (campaigns first) for at most `wait_s`."""
    stop = stop or threading.Event()
    poll_s = POLL_S if poll_s is None else poll_s
    rec = update(cfg, job_id, pid=os.getpid(), status="waiting")
    if rec is None:
        raise ValueError(f"no gate job {job_id}")
    pools = worker_pools(cfg)
    if not pools:
        return _finish(cfg, job_id, "unavailable",
                       error="box unavailable: no placeable gate worker (nw workers list)")
    by = rec.get("requester") or "cli"
    pcfg = {"project": rec.get("project"), "gates": rec["gate_cfg"]}
    state = _state(job_id, rec.get("project"), rec["repo"], rec["base"], rec["commit"])
    jdir = job_dir(cfg, job_id)
    wait_ev, done = threading.Event(), threading.Event()
    t0 = time.monotonic()
    threading.Thread(target=_deadline_watch, args=(stop, wait_ev, t0 + wait_s, done),
                     daemon=True).start()
    fds = []
    try:
        for d, n in ((requester_tokens(cfg, by), settings(cfg)["max_per_requester"]),
                     (share_tokens(cfg), max_slots(cfg))):
            fd = _wait_token(d, n, wait_ev, poll_s)
            if fd is None:
                break
            fds.append(fd)
        if len(fds) < 2:
            done.set()
            return _ended_waiting(cfg, job_id, stop, wait_s, "a gate-job token")
        holder = {"job": job_id, "by": by, "project": rec.get("project"),
                  "branch": rec.get("branch")}
        with GS.acquire(cfg, job_id, stop=wait_ev, poll_s=poll_s, pools=pools,
                        skip=lambda _pool: campaign_waiting(cfg),
                        repool=lambda: worker_pools(cfg), holder=holder) as hold:
            done.set()
            if hold.interrupted or hold.pool is None:
                return _ended_waiting(cfg, job_id, stop, wait_s, "a worker gate slot")
            rec = update(cfg, job_id, status="running", worker=hold.pool,
                         started_at=now_iso(), waited_s=round(time.monotonic() - t0, 1))
            _event(cfg, rec)
            try:
                run = RG.run_detached(hold.pool, pcfg, state, cfg, SEQ, jdir / "gates",
                                      stop=stop, keep_changed=True)
            except RG.RemoteFailure as f:
                return _finish(cfg, job_id, "transport_error",
                               error=f"transport failure on {hold.pool} ({f.stage}): "
                                     f"{f.error}"[:2000])
            if run is None or run.interrupted or stop.is_set():
                return _finish(cfg, job_id, "cancelled", error="cancelled while running",
                               result=_rows(run, jdir) if run else [])
            ok = bool(run.results) and all(r.ok for r in run.results if r.required)
            return _finish(cfg, job_id, "passed" if ok else "failed", result=_rows(run, jdir),
                           failing=[] if ok else _failing(run.results),
                           changed=list(run.changed) if run.changed else None)
    finally:
        done.set()
        for fd in fds:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _ended_waiting(cfg, job_id, stop, wait_s, what) -> dict:
    if stop.is_set():
        return _finish(cfg, job_id, "cancelled", error="cancelled while waiting")
    return _finish(cfg, job_id, "unavailable",
                   error=f"box unavailable: no {what} within {wait_s:g}s"
                         + (" (campaign gates go first)" if what.endswith("slot") else ""))


def follow(cfg, job_id: str, *, stop: threading.Event | None = None,
           poll_s: float = POLL_S) -> dict:
    """Wait for another process's job to end (a reused job under `--wait`); its record."""
    stop = stop or threading.Event()
    while True:
        rec = read(cfg, job_id) or {}
        st = status_of(rec)
        if st in FINAL or st == "lost" or stop.is_set():
            return rec
        stop.wait(poll_s)


def payload(cfg, rec: dict, *, reused: bool = False) -> dict:
    """The `nw.gatejob.v1` view of a record."""
    st = status_of(rec)
    jdir = job_dir(cfg, rec["id"])
    return {"schema": SCHEMA, "id": rec["id"], "status": st, "exit": EXIT.get(st),
            "gates": rec.get("result") or [], "failing": rec.get("failing") or [],
            "log_dir": str(jdir / "gates"), "worker": rec.get("worker"),
            "duration": rec.get("duration"), "project": rec.get("project"),
            "branch": rec.get("branch"), "commit": rec.get("commit"),
            "requester": rec.get("requester"), "reused": reused, "error": rec.get("error"),
            "changed": rec.get("changed"), "skipped": rec.get("skipped") or []}
