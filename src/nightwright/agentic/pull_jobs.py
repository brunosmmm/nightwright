"""Pull workers (SPEC-0334): the coordinator's job API and its side of a pull job.

A pull worker dials out instead of being SSHed into. For such a worker the gate or persona step
queues its job (`<agentic_dir>/job-queue/<job_id>.json` + `.bundle`) instead of `nw worker
submit`, then waits on the job record (`remote_jobs`), polling it every second. `nw serve --jobs`
(`make_server`) is the only writer of a pull job's frames and of its record's pull fields: it
leases the job to its worker with a header signed under that worker's secret (HMAC-SHA256),
applies the frames the worker posts in `n` order under a per-job file lock (a gate job's through
SPEC-0341's `_Job.feed`, so the gate files and the record's cursor are written exactly as for an
SSH attach), spools them to `<agentic_dir>/job-frames/<job_id>.jsonl`, and gives a lease up
(`pull_state: lost`) when it is not renewed within `job_lease_s`. The waiting step turns a
`done` into the run (replaying the spool), an `error` into SPEC-0338's fallback and a `lost`
into SPEC-0342's give-up (retry on another worker, or local).

Each pull worker has its own bearer token (only its sha256 is in `workers.json`) and its own
signing secret (`<config_dir>/secrets/workers/<name>.key`, mode 0600). stdlib only."""
from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import hmac
import http.server
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from ..export import _atomic_write
from . import remote_jobs as RJ
from . import store
from . import workers as WK

DEFAULT_BIND = "127.0.0.1:8790"
LEASE_WAIT_S = 30.0                 # the longest `POST /v1/lease` long-poll
LEASE_CHECK_S = 5.0                 # how often the server looks for expired leases
WAIT_TICK_S = 1.0                   # the waiting step's record poll
EXPIRES_S = 15 * 60                 # a signed header's `expires_at` - `issued_at`
CLOCK_SKEW_S = 120.0
MAX_FRAMES_BYTES = 4 << 20          # a frames batch (and any other JSON body)
MAX_BUNDLE_BYTES = 64 << 20         # a result bundle
TAILSCALE_NETS = (ipaddress.ip_network("100.64.0.0/10"),
                  ipaddress.ip_network("fd7a:115c:a1e0::/48"))
SIGNED_FIELDS = ("issued_at", "expires_at", "nonce", "bundle_sha256", "sig")
_CHUNK = 1 << 16
_HEX64 = re.compile(r"^[0-9a-f]{64}\Z")
_JOB_PATH = re.compile(r"^/v1/jobs/(?P<id>[A-Za-z0-9._-]+)/(?P<what>bundle|frames|result-bundle)\Z")


class PullError(Exception):
    """A request the API refuses: `status` is the HTTP status, `body` the JSON answer."""

    def __init__(self, status: int, error: str, **extra):
        super().__init__(error)
        self.status, self.body = status, {"error": error, **extra}


# ---- credentials -----------------------------------------------------------------------------

def is_pull(worker: dict | None) -> bool:
    return isinstance(worker, dict) and worker.get("mode") == "pull"


def secret_ref(name: str) -> str:
    return f"secrets/workers/{name}.key"


def secret_path(cfg, name: str) -> Path:
    return Path(cfg["config_dir"]) / secret_ref(name)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def write_private(path: Path, text: str) -> None:
    """Write `text` to `path` with mode 0600 from the start (parents 0700), replacing it."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)


def new_credential(cfg, name: str) -> tuple[str, str]:
    """A fresh `(token, secret)` for worker `name`; the secret is written to its 0600 file (the
    caller stores only `token_hash(token)` in the registry)."""
    token, secret = secrets.token_urlsafe(32), secrets.token_hex(32)
    write_private(secret_path(cfg, name), secret + "\n")
    return token, secret


def drop_credential(cfg, name: str) -> None:
    secret_path(cfg, name).unlink(missing_ok=True)


def read_secret(cfg, name: str) -> str | None:
    try:
        s = secret_path(cfg, name).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return s or None


def coordinator_url(cfg) -> str:
    """`agentic.workers.coordinator_url`, else the default bind over http."""
    raw = ((cfg.get("agentic") or {}).get("workers") or {})
    url = raw.get("coordinator_url") if isinstance(raw, dict) else None
    return url if isinstance(url, str) and url else f"http://{DEFAULT_BIND}"


def enrol_blob(url: str, name: str, token: str, secret: str) -> str:
    doc = {"coordinator_url": url, "name": name, "token": token, "secret": secret}
    return base64.b64encode(json.dumps(doc, sort_keys=True).encode("utf-8")).decode("ascii")


def parse_blob(blob: str) -> dict:
    """The enrolment blob's document; ValueError when it is not one."""
    try:
        doc = json.loads(base64.b64decode(blob.strip(), validate=True).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise ValueError(f"not an enrolment blob: {e}")
    if not isinstance(doc, dict) or not all(isinstance(doc.get(k), str) and doc[k]
                                            for k in ("coordinator_url", "name", "token",
                                                      "secret")):
        raise ValueError("not an enrolment blob: it needs coordinator_url, name, token, secret")
    return doc


# ---- signed headers --------------------------------------------------------------------------

def canonical(doc: dict) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _mac(secret: str, header: dict) -> str:
    body = {k: v for k, v in header.items() if k != "sig"}
    return hmac.new(secret.encode("utf-8"), canonical(body), hashlib.sha256).hexdigest()


def sign(secret: str, header: dict, bundle_sha256: str, now: float | None = None) -> dict:
    """The header as leased: `issued_at` (now), `expires_at` (+15 min), `nonce` (the job id),
    `bundle_sha256`, then `sig` over all of it."""
    now = time.time() if now is None else now
    h = {k: v for k, v in header.items() if k not in SIGNED_FIELDS}
    h.update(issued_at=int(now), expires_at=int(now) + EXPIRES_S, nonce=h["job_id"],
             bundle_sha256=bundle_sha256)
    h["sig"] = _mac(secret, h)
    return h


def verify(secret: str, header: dict, bundle: bytes, seen, now: float | None = None) -> str | None:
    """Why the worker must refuse this leased job, else None. `seen(nonce)` is True for a nonce
    already used. Checked once, before the job starts."""
    now = time.time() if now is None else now
    sig = header.get("sig") if isinstance(header, dict) else None
    if not isinstance(sig, str) or not hmac.compare_digest(sig, _mac(secret, header)):
        return "bad signature"
    exp = header.get("expires_at")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool) or now > exp + CLOCK_SKEW_S:
        return "the job has expired"
    nonce = header.get("nonce")
    if not isinstance(nonce, str) or nonce != header.get("job_id") or seen(nonce):
        return "replayed nonce"
    if hashlib.sha256(bundle).hexdigest() != header.get("bundle_sha256"):
        return "bundle hash mismatch"
    return None


# ---- the coordinator's job store -------------------------------------------------------------

def queue_dir(cfg) -> Path:
    return store.agentic_dir(cfg) / "job-queue"


def uploads_dir(cfg) -> Path:
    return store.agentic_dir(cfg) / "job-uploads"


def frames_dir(cfg) -> Path:
    return store.agentic_dir(cfg) / "job-frames"


def spool_path(cfg, job_id: str) -> Path:
    return frames_dir(cfg) / f"{job_id}.jsonl"


def result_bundle_path(cfg, job_id: str) -> Path:
    return uploads_dir(cfg) / f"{job_id}.result.bundle"


@contextmanager
def _flock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def job_lock(cfg, job_id: str):
    """The per-job lock every apply of a pull job's frames runs under."""
    return _flock(frames_dir(cfg) / f"{job_id}.lock")


def _queue_lock(cfg):
    return _flock(queue_dir(cfg) / ".lock")


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def submit(cfg, worker: dict, header: dict, bundle: Path) -> None:
    """Queue the job for its pull worker (in place of `nw worker submit`): the bundle, then the
    queue entry, then the record's `pull_state: queued`."""
    qd = queue_dir(cfg)
    qd.mkdir(parents=True, exist_ok=True)
    job_id = header["job_id"]
    dest = qd / f"{job_id}.bundle"
    if header.get("bundle_bytes"):
        shutil.copyfile(bundle, dest)
    else:
        dest.write_bytes(b"")
    entry = {"header": header, "worker": worker["name"], "kind": header["kind"],
             "bundle_sha256": _sha_file(dest), "queued_at": store.now_iso()}
    _atomic_write(qd / f"{job_id}.json", json.dumps(entry, sort_keys=True) + "\n")
    RJ.update(cfg, job_id, pull=True, pull_state="queued", pull_cursor=0)


def unqueue(cfg, job_id: str) -> None:
    """Drop a job's queue entry and bundle (a final or abandoned job)."""
    with _queue_lock(cfg):
        for suffix in (".json", ".bundle"):
            (queue_dir(cfg) / f"{job_id}{suffix}").unlink(missing_ok=True)


def request_cancel(cfg, job_id: str) -> None:
    """SPEC-0229 / SPEC-0341 cancel of a pull job: `cancel_requested`, which the server returns
    as `cancel: true` on the worker's next frames or heartbeat; a job never leased is unqueued."""
    rec = RJ.update(cfg, job_id, cancel_requested=True)
    if rec is not None and rec.get("pull_state") == "queued":
        unqueue(cfg, job_id)


def give_up_lease(cfg, job_id: str, reason: str = "lease") -> None:
    """The job's lease is gone (expired, or its worker revoked): `pull_state: lost`, which the
    waiting step turns into SPEC-0342's give-up. A later request for it gets 410."""
    RJ.update(cfg, job_id, pull_state="lost", pull_lost_reason=reason)
    unqueue(cfg, job_id)


def revoke_worker(cfg, name: str) -> list[str]:
    """`nw workers rm` of a pull worker: its live pull jobs are lost (requeued or run locally by
    their steps). Returns the job ids."""
    out = []
    for rec in RJ.live(cfg):
        if rec.get("pull") and rec.get("worker") == name \
                and rec.get("pull_state") not in ("done", "error", "lost"):
            give_up_lease(cfg, rec["job_id"], "revoked")
            out.append(rec["job_id"])
    return out


def read_spool(cfg, job_id: str) -> list[dict]:
    try:
        text = spool_path(cfg, job_id).read_text(encoding="utf-8")
    except OSError:
        return []
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


def drop_job_files(cfg, job_id: str) -> None:
    """A finished pull job's spool, lock, upload and queue files."""
    unqueue(cfg, job_id)
    for p in (spool_path(cfg, job_id), frames_dir(cfg) / f"{job_id}.lock",
              result_bundle_path(cfg, job_id)):
        p.unlink(missing_ok=True)


# ---- the waiting step: gates -----------------------------------------------------------------

def _since(rec: dict, key: str) -> float:
    return RJ.epoch(rec.get(key)) or time.time()


def follow_gate(cfg, worker: dict, rec: dict, project_cfg: dict, seq: int, stop):
    """`remote_gates._follow` for a pull worker: poll the record every second until the server
    marks the job `done` (the run, replayed from the spool), `error` (a `RemoteFailure`: the
    fallback) or `lost` (`_GiveUp`). A job never leased within `worker_ttl_s` is `unreachable`;
    one past its `deadline_at` is `deadline`. A stop cancels the job (`cancel: true` reaches the
    worker) and returns an interrupted run; a code-change detach leaves the record live for the
    next runner, which re-enters this wait (adoption)."""
    from . import gates as G
    from . import remote_gates as RG
    job_id = rec["job_id"]
    ttl = RJ.settings(cfg)["worker_ttl_s"]
    deadline = RJ.epoch(rec.get("deadline_at")) or (time.time() + RG.MAX_JOB_TIMEOUT_S)
    queued_since = time.time()                  # an adopted job's TTL counts from the adoption
    detach_ev = threading.Event()
    with RG._ATTACHED_LOCK:
        RG._ATTACHED[rec["campaign"]] = detach_ev

    def interrupted():
        return RG.Outcome(G.GateRun(seq, False, (), store.now_iso(), 0.0, interrupted=True,
                                    worker=worker["name"], job_id=job_id))
    try:
        while True:
            cur = RJ.read(cfg, job_id)
            if cur is None:
                raise RG._GiveUp("lost", worker, rec)
            ps = cur.get("pull_state")
            if cur.get("state") == "cancelled":
                return interrupted()
            if ps == "done":
                return _replay_gate(cfg, worker, cur, project_cfg, seq)
            if ps == "error":
                err = cur.get("pull_error") or {}
                raise RG.RemoteFailure(str(err.get("stage") or "error"),
                                       str(err.get("error") or ""),
                                       cooldown=bool(err.get("cooldown", True)))
            if ps == "lost":
                raise RG._GiveUp("lost", worker, rec)
            if ps == "queued" and time.time() - queued_since > ttl:
                unqueue(cfg, job_id)
                raise RG._GiveUp("unreachable", worker, rec)
            if time.time() > deadline:
                raise RG._GiveUp("deadline", worker, rec)
            if G._stop_requested(stop):         # SPEC-0229 stop: the worker is told to cancel
                RJ.update(cfg, job_id, state="cancelled")
                request_cancel(cfg, job_id)
                return interrupted()
            if detach_ev.wait(WAIT_TICK_S):     # a code-change detach: adopted after the exec
                RJ.update(cfg, job_id, state="detached")
                return interrupted()
    except RG.RemoteFailure:
        if RG._leave(rec["campaign"], detach_ev):
            RJ.update(cfg, job_id, state="detached")
            return interrupted()
        raise
    finally:
        RG._leave(rec["campaign"], detach_ev)


def _replay_gate(cfg, worker: dict, rec: dict, project_cfg: dict, seq: int):
    """The server applied every frame (files and cursor): rebuild the `GateRun` from the spool
    through the same `_Job.feed` (frames at or below the cursor write nothing)."""
    from . import remote_gates as RG
    job = RG._Job(cfg, worker, rec, project_cfg, seq)
    for frame in read_spool(cfg, rec["job_id"]):
        run = job.feed(frame)
        if run is not None:
            return RG.Outcome(run, job.worker_wait)
    raise RG.RemoteFailure("protocol", "the job is done but its spool has no `done` frame")


# ---- the waiting step: personas --------------------------------------------------------------

def follow_persona(rr, req, hooks, rec: dict):
    """`remote_personas._follow` for a pull worker: poll the record every second; `done` hands
    the spooled `done` frame (its result bundle read back from the upload) to `_finish`, the
    same write-back as SSH; `error`/`lost`/unleased/deadline go to `_lost`; a detach detaches."""
    from . import remote_gates as RG
    from . import remote_personas as RPS
    from . import runner as R
    from . import runproc as RP
    cfg, job_id = rr.cfg, rec["job_id"]
    ttl = RJ.settings(cfg)["worker_ttl_s"]
    deadline = RJ.epoch(rec.get("deadline_at")) or (time.time() + RG.MAX_JOB_TIMEOUT_S)
    queued_since, progress = time.time(), None
    while True:
        if hooks is not None and hooks.detached():
            RJ.update(cfg, job_id, state="detached")
            return R.DETACHED
        cur = RJ.read(cfg, job_id)
        if cur is None or cur.get("pull_state") == "lost":
            return RPS._lost(rr, req, hooks, rec, "lost")
        if cur.get("state") == "cancelled":
            return RPS._lost(rr, req, hooks, rec, "cancelled")
        if cur.get("progress") and cur["progress"] != progress:
            progress = cur["progress"]
            with contextlib.suppress(OSError):
                RP.write_json_atomic(Path(req.run_dir) / "progress.json", progress)
        ps = cur.get("pull_state")
        if ps == "done":
            frame = next((f for f in reversed(read_spool(cfg, job_id))
                          if f.get("type") == "done"), None)
            if frame is not None:
                frame = _with_bundle(cfg, job_id, frame)
                out = RPS._finish(rr, req, hooks, cur, frame)
                drop_job_files(cfg, job_id)
                return out
            return RPS._lost(rr, req, hooks, rec, "lost")
        if ps == "error":
            err = cur.get("pull_error") or {}
            return RPS._lost(rr, req, hooks, rec, f"error: {err.get('stage')}: {err.get('error')}")
        if ps == "queued" and time.time() - queued_since > ttl:
            unqueue(cfg, job_id)
            return RPS._lost(rr, req, hooks, rec, "unreachable")
        if time.time() > deadline:
            return RPS._lost(rr, req, hooks, rec, "deadline")
        time.sleep(WAIT_TICK_S)


def _with_bundle(cfg, job_id: str, frame: dict) -> dict:
    """The `done` frame as SSH would carry it: the uploaded result bundle inlined again."""
    want = frame.get("result_bundle_sha256")
    bundle = dict(frame.get("bundle") or {})
    if want:
        p = result_bundle_path(cfg, job_id)
        data = p.read_bytes() if p.is_file() else b""
        if hashlib.sha256(data).hexdigest() == want:
            bundle["bundle_b64"] = base64.b64encode(data).decode("ascii")
    return {**frame, "bundle": bundle or frame.get("bundle")}


# ---- the server's apply path -----------------------------------------------------------------

def _job_for(cfg, worker: str, job_id: str) -> dict:
    """The job's record, when `worker` may touch it: 410 for an unknown job or one whose lease
    was given up, 403 for another worker's."""
    rec = RJ.read(cfg, job_id)
    if rec is None or not rec.get("pull"):
        raise PullError(410, f"job {job_id} is gone")
    if rec.get("worker") != worker:
        raise PullError(403, f"job {job_id} is not leased to {worker}")
    if rec.get("pull_state") == "lost" or rec.get("state") in ("lost", "local"):
        raise PullError(410, f"job {job_id} is gone")
    return rec


def _cancel_flag(rec: dict) -> bool:
    return bool(rec.get("cancel_requested")) or rec.get("state") == "cancelled"


def _renewed(cfg, rec: dict, **fields) -> dict:
    now = time.time()
    return RJ.update(cfg, rec["job_id"], last_seen_at=RJ.iso(now),
                     lease_until=now + RJ.settings(cfg)["job_lease_s"], **fields) or rec


def apply_frames(cfg, worker: str, job_id: str, frames: list) -> dict:
    """`POST /v1/jobs/<id>/frames`: apply the frames after the record's `pull_cursor`, in `n`
    order, stopping at a gap. A gate job's frames go through `_Job.feed` (its files and cursor);
    a `done` whose `result_bundle_sha256` was not uploaded yet is refused with 409 and the
    cursor unchanged. Returns `{cursor, cancel, job_lease_s}`."""
    if not isinstance(frames, list) or not all(isinstance(f, dict) for f in frames):
        raise PullError(400, "`frames` must be a list of objects")
    _job_for(cfg, worker, job_id)
    with job_lock(cfg, job_id):
        rec = _job_for(cfg, worker, job_id)
        cursor = int(rec.get("pull_cursor") or 0)
        fields: dict = {}
        if rec.get("pull_state") in ("done", "error"):
            frames = []                         # final: a re-post only learns the cursor
        job = None
        for frame in sorted(frames, key=lambda f: f.get("n") if isinstance(f.get("n"), int)
                            else 0):
            n = frame.get("n")
            if not isinstance(n, int) or isinstance(n, bool) or n < 1:
                raise PullError(400, "every frame needs an integer `n` from 1")
            if n <= cursor:
                continue
            if n != cursor + 1:
                break
            if frame.get("type") == "done" and frame.get("result_bundle_sha256"):
                p = result_bundle_path(cfg, job_id)
                if not p.is_file() or _sha_file(p) != frame["result_bundle_sha256"]:
                    raise PullError(409, "the result bundle is not uploaded yet", cursor=cursor)
            if rec.get("kind") == "gate":
                job = job or _gate_job(cfg, worker, rec)
                fields.update(_feed_gate(job, frame))
            elif frame.get("type") == "error":
                fields.update(pull_state="error", pull_error={
                    "stage": str(frame.get("stage") or "error"),
                    "error": str(frame.get("error") or ""), "cooldown": True})
            elif frame.get("type") == "progress":
                fields["progress"] = {k: frame.get(k)
                                      for k in ("ts", "transcript_bytes", "last_line_ts")}
            if frame.get("type") == "done" and "pull_state" not in fields:
                fields["pull_state"] = "done"
            with open(spool_path(cfg, job_id), "a", encoding="utf-8") as f:
                f.write(json.dumps(frame, ensure_ascii=False) + "\n")
            cursor = n
            if rec.get("kind") != "gate":
                fields.update(cursor=n, state="attached")
            if fields.get("pull_state") in ("done", "error"):
                break
        rec = _renewed(cfg, rec, pull_cursor=cursor, **fields)
    if rec.get("pull_state") in ("done", "error"):
        unqueue(cfg, job_id)
    return {"cursor": cursor, "cancel": _cancel_flag(rec),
            "job_lease_s": RJ.settings(cfg)["job_lease_s"]}


def _gate_job(cfg, worker: str, rec: dict):
    """A `_Job` for the record, brought up to its cursor by replaying the spool (frames at or
    below the cursor rebuild results and write nothing)."""
    from . import remote_gates as RG
    job = RG._Job(cfg, {"name": worker}, rec, {}, int(rec.get("seq_or_run") or 0))
    for frame in read_spool(cfg, rec["job_id"]):
        try:
            job.feed(frame)
        except RG.RemoteFailure:
            pass
    return job


def _feed_gate(job, frame: dict) -> dict:
    from . import remote_gates as RG
    try:
        run = job.feed(frame)
    except RG.RemoteFailure as e:
        return {"pull_state": "error",
                "pull_error": {"stage": e.stage, "error": e.error, "cooldown": e.cooldown}}
    return {"pull_state": "done"} if run is not None else {}


def store_result_bundle(cfg, worker: str, job_id: str, tmp: Path, sha: str) -> dict:
    """`PUT /v1/jobs/<id>/result-bundle`: the hashed upload becomes the job's result bundle."""
    _job_for(cfg, worker, job_id)
    with job_lock(cfg, job_id):
        os.replace(tmp, result_bundle_path(cfg, job_id))
    RJ.update(cfg, job_id, result_bundle_sha256=sha)
    return {"sha256": sha}


def heartbeat(cfg, worker: str, jobs: list) -> dict:
    """`POST /v1/heartbeat`: renew the listed jobs' leases; per job `cancel` and `gone`."""
    if not isinstance(jobs, list):
        raise PullError(400, "`jobs` must be a list")
    out = {}
    for j in jobs:
        job_id = j.get("id") if isinstance(j, dict) else None
        if not isinstance(job_id, str):
            continue
        try:
            rec = _renewed(cfg, _job_for(cfg, worker, job_id))
            out[job_id] = {"cancel": _cancel_flag(rec), "gone": False}
        except PullError as e:
            out[job_id] = {"cancel": e.status == 410, "gone": e.status == 410}
    return {"jobs": out, "job_lease_s": RJ.settings(cfg)["job_lease_s"]}


def lease(cfg, worker: dict, kinds, slots_free, wait_s: float, stop: threading.Event) -> dict | None:
    """`POST /v1/lease`: the oldest queued job for `worker` of one of `kinds`, signed, else None
    after `wait_s` (at most `LEASE_WAIT_S`)."""
    if not isinstance(kinds, list) or not isinstance(slots_free, int) or slots_free < 1 \
            or not worker.get("enabled", True):
        return None
    end = time.monotonic() + max(0.0, min(float(wait_s), LEASE_WAIT_S))
    while True:
        got = _take(cfg, worker, kinds)
        if got is not None or time.monotonic() >= end or stop.is_set():
            return got
        stop.wait(0.25)


def _take(cfg, worker: dict, kinds) -> dict | None:
    qd = queue_dir(cfg)
    if not qd.is_dir():
        return None
    secret = read_secret(cfg, worker["name"])
    if secret is None:
        return None
    with _queue_lock(cfg):
        entries = []
        for p in qd.glob("*.json"):
            try:
                e = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if e.get("worker") == worker["name"] and e.get("kind") in kinds:
                entries.append((str(e.get("queued_at")), p, e))
        for _at, p, e in sorted(entries, key=lambda t: t[0]):
            job_id = e["header"]["job_id"]
            rec = RJ.read(cfg, job_id)
            p.unlink(missing_ok=True)           # leased (or stale): never handed out twice
            if rec is None or rec.get("state") not in RJ.LIVE or _cancel_flag(rec):
                (qd / f"{job_id}.bundle").unlink(missing_ok=True)
                continue
            header = sign(secret, e["header"], e["bundle_sha256"])
            now = time.time()
            RJ.update(cfg, job_id, pull_state="leased", accepted_at=RJ.iso(now),
                      leased_at=RJ.iso(now), last_seen_at=RJ.iso(now),
                      lease_until=now + RJ.settings(cfg)["job_lease_s"])
            return {"job": header, "bundle_sha256": e["bundle_sha256"],
                    "job_lease_s": RJ.settings(cfg)["job_lease_s"]}
    return None


def expire_leases(cfg, now: float | None = None) -> list[str]:
    """Every live pull job whose lease passed: given up (`pull_state: lost`). Returns the ids."""
    now = time.time() if now is None else now
    out = []
    for rec in RJ.live(cfg):
        until = rec.get("lease_until")
        if rec.get("pull") and rec.get("pull_state") == "leased" \
                and isinstance(until, (int, float)) and now > until:
            give_up_lease(cfg, rec["job_id"], "lease")
            out.append(rec["job_id"])
    return out


# ---- the HTTP server -------------------------------------------------------------------------

def check_bind(host: str, tls: bool) -> str | None:
    """Why `host` may not be bound, else None: without TLS only loopback and the Tailscale
    ranges; a wildcard address never without TLS."""
    if tls:
        return None
    if host == "localhost":
        return None
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return f"{host!r} is not an IP address; without TLS bind a loopback or Tailscale address"
    if ip.is_unspecified:
        return f"{host} is a wildcard bind; it needs --tls-cert/--tls-key"
    if ip.is_loopback or any(ip.version == n.version and ip in n for n in TAILSCALE_NETS):
        return None
    return (f"{host} is neither loopback nor Tailscale (100.64.0.0/10, fd7a:115c:a1e0::/48); "
            "it needs --tls-cert/--tls-key")


def split_bind(bind: str) -> tuple[str, int]:
    host, sep, port = bind.rpartition(":")
    if not sep or not port.isdigit():
        raise ValueError(f"--bind {bind!r} is not ADDR:PORT")
    return host.strip("[]"), int(port)


class PullServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, cfg, addr, *, client_ca: bool = False):
        self.cfg, self.client_ca = cfg, client_ca
        self.stopping = threading.Event()
        if ":" in addr[0]:
            self.address_family = socket.AF_INET6
        super().__init__(addr, _Handler)

    def check_leases(self, every: float = LEASE_CHECK_S) -> None:
        while not self.stopping.wait(every):
            with contextlib.suppress(Exception):
                expire_leases(self.cfg)

    def shutdown(self):
        self.stopping.set()
        super().shutdown()


def make_server(cfg, bind: str = DEFAULT_BIND, *, tls_cert=None, tls_key=None,
                client_ca=None, check_every: float = LEASE_CHECK_S) -> PullServer:
    """The API server, bound (not yet serving) with its lease checker started; ValueError for a
    refused bind or a TLS option without its pair."""
    host, port = split_bind(bind)
    if bool(tls_cert) != bool(tls_key):
        raise ValueError("--tls-cert and --tls-key go together")
    if client_ca and not tls_cert:
        raise ValueError("--client-ca needs --tls-cert/--tls-key")
    why = check_bind(host, bool(tls_cert))
    if why:
        raise ValueError(f"refusing to bind: {why}")
    for d in (queue_dir(cfg), uploads_dir(cfg), frames_dir(cfg)):
        d.mkdir(parents=True, exist_ok=True)
    srv = PullServer(cfg, (host, port), client_ca=bool(client_ca))
    if tls_cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(tls_cert, tls_key)
        if client_ca:
            ctx.load_verify_locations(client_ca)
            ctx.verify_mode = ssl.CERT_REQUIRED
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.check_leases, args=(check_every,), daemon=True).start()
    return srv


def _worker_by_token(cfg, token: str | None) -> dict | None:
    """The pull worker the bearer token belongs to (compared in constant time against every
    pull worker's `token_sha256`), else None. A disabled worker still authenticates (it only
    gets no new lease): `off` is not a revocation."""
    got = token_hash(token or "")
    match = None
    for w in WK.load(cfg):
        want = w.get("token_sha256") if is_pull(w) else None
        if isinstance(want, str) and hmac.compare_digest(got, want) and token:
            match = w
    return match


class _Handler(http.server.BaseHTTPRequestHandler):
    server: PullServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):         # quiet: the queue's events are the record
        if os.environ.get("NW_SERVE_LOG"):
            super().log_message(fmt, *args)

    def _send(self, status: int, body: dict | None = None) -> None:
        data = b"" if body is None else json.dumps(body).encode("utf-8")
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _auth(self) -> dict:
        auth = self.headers.get("Authorization") or ""
        token = auth[7:].strip() if auth.startswith("Bearer ") else None
        w = _worker_by_token(self.server.cfg, token)
        if w is None:
            raise PullError(401, "unknown or revoked token")
        if self.server.client_ca:
            cert = self.connection.getpeercert() or {}
            cn = next((v for rdn in cert.get("subject", ()) for k, v in rdn
                       if k == "commonName"), None)
            if cn != w["name"]:
                raise PullError(403, "the client certificate's CN is not this worker")
        return w

    def _body(self, cap: int) -> tuple[Path, str]:
        """The request body streamed to a temp file under `job-uploads/`, and its sha256."""
        raw = self.headers.get("Content-Length")
        if raw is None or not raw.isdigit():
            raise PullError(411, "Content-Length required")
        n = int(raw)
        if n > cap:
            self.close_connection = True
            raise PullError(413, f"body over {cap} bytes")
        d = uploads_dir(self.server.cfg)
        d.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=d, prefix=".up-")
        h = hashlib.sha256()
        try:
            with os.fdopen(fd, "wb") as f:
                left = n
                while left:
                    chunk = self.rfile.read(min(left, _CHUNK))
                    if not chunk:
                        raise PullError(400, "body shorter than Content-Length")
                    h.update(chunk)
                    f.write(chunk)
                    left -= len(chunk)
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise
        return Path(name), h.hexdigest()

    def _json(self) -> dict:
        tmp, _sha = self._body(MAX_FRAMES_BYTES)
        try:
            doc = json.loads(tmp.read_text(encoding="utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            raise PullError(400, "the body is not JSON")
        finally:
            tmp.unlink(missing_ok=True)
        if not isinstance(doc, dict):
            raise PullError(400, "the body is not a JSON object")
        return doc

    def _handle(self, method: str) -> None:
        cfg = self.server.cfg
        try:
            path = urlsplit(self.path).path
            m = _JOB_PATH.match(path)
            known = (method == "POST" and path in ("/v1/lease", "/v1/heartbeat")) or (
                m is not None and (method, m.group("what")) in (
                    ("GET", "bundle"), ("POST", "frames"), ("PUT", "result-bundle")))
            if not known:
                raise PullError(404, "no such endpoint")
            w = self._auth()
            if path == "/v1/lease":
                doc = self._json()
                got = lease(cfg, w, doc.get("kinds"), doc.get("slots_free"),
                            doc.get("wait_s", LEASE_WAIT_S), self.server.stopping)
                return self._send(204) if got is None else self._send(200, got)
            if path == "/v1/heartbeat":
                return self._send(200, heartbeat(cfg, w["name"], self._json().get("jobs")))
            job_id, what = m.group("id"), m.group("what")
            if what == "frames":
                return self._send(200, apply_frames(cfg, w["name"], job_id,
                                                    self._json().get("frames")))
            if what == "result-bundle":
                _job_for(cfg, w["name"], job_id)
                tmp, sha = self._body(MAX_BUNDLE_BYTES)
                try:
                    return self._send(200, store_result_bundle(cfg, w["name"], job_id, tmp, sha))
                finally:
                    tmp.unlink(missing_ok=True)
            return self._bundle(w, job_id)
        except PullError as e:
            self.close_connection = True        # the body may be unread
            self._send(e.status, e.body)
        except (OSError, ValueError) as e:
            self.close_connection = True
            with contextlib.suppress(OSError):
                self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def _bundle(self, w: dict, job_id: str) -> None:
        _job_for(self.server.cfg, w["name"], job_id)
        p = queue_dir(self.server.cfg) / f"{job_id}.bundle"
        if not p.is_file():
            raise PullError(410, f"job {job_id} has no bundle any more")
        size = p.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with open(p, "rb") as f:
            shutil.copyfileobj(f, self.wfile, _CHUNK)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

