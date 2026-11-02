"""Durable job records for async remote gate jobs (SPEC-0341): `<agentic_dir>/remote-jobs/<job_id>.json`.

A record says which worker runs the job, its attempt, the gate seq, the last applied frame `n`
(`cursor`), its state and its deadline. It outlives the coordinator, so a restarted queue finds
the campaign's live record before it allocates a new gate seq and adopts the job instead of
re-running it. Writes are atomic and serialized by one flock; a final state is never
overwritten by a non-final one (a cancel landing while the step still attaches wins)."""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import math
import os
import re
from contextlib import contextmanager
from pathlib import Path

from ..export import _atomic_write
from .store import agentic_dir

SCHEMA = "nw.remote_job.v1"
LIVE = ("submitted", "attached", "detached")
FINAL = ("done", "lost", "cancelled", "local")
STATES = LIVE + FINAL
DEFAULT_WORKER_TTL_S = 300.0        # `agentic.workers.worker_ttl_s`
DEFAULT_JOB_LEASE_S = 900.0         # `agentic.workers.job_lease_s` (SPEC-0340's default)
_GATE_ID = re.compile(r"^(?P<cid>.+)-gate-(?P<seq>[0-9]{3})-a(?P<attempt>[0-9]+)\Z")


def jobs_dir(cfg) -> Path:
    return agentic_dir(cfg) / "remote-jobs"


def path(cfg, job_id: str) -> Path:
    return jobs_dir(cfg) / f"{job_id}.json"


def gate_job_id(campaign_id: str, seq: int, attempt: int = 1) -> str:
    return f"{campaign_id}-gate-{seq:03d}-a{attempt}"


def settings(cfg) -> dict:
    """`{worker_ttl_s, job_lease_s}` from `agentic.workers`; anything but a positive finite
    number falls back to the default."""
    raw = (cfg.get("agentic") or {}).get("workers")
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for key, default in (("worker_ttl_s", DEFAULT_WORKER_TTL_S),
                         ("job_lease_s", DEFAULT_JOB_LEASE_S)):
        v = raw.get(key)
        ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) \
            and v > 0
        out[key] = float(v) if ok else default
    return out


def has_async(worker: dict) -> bool:
    """The worker's cached `info` (written by `nw workers check`) lists the `async` feature. No
    cached `info` reads as an old worker."""
    info = worker.get("info")
    feats = info.get("features") if isinstance(info, dict) else None
    return isinstance(feats, list) and "async" in feats


def epoch(ts) -> float | None:
    """An ISO timestamp as epoch seconds; None when unreadable."""
    try:
        t = dt.datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    return (t if t.tzinfo else t.astimezone()).timestamp()


def iso(epoch_s: float) -> str:
    return dt.datetime.fromtimestamp(epoch_s).astimezone().isoformat(timespec="seconds")


@contextmanager
def _lock(cfg):
    d = jobs_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(d / ".lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def read(cfg, job_id: str) -> dict | None:
    try:
        rec = json.loads(path(cfg, job_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) and rec.get("schema") == SCHEMA else None


def _write(cfg, rec: dict) -> None:
    _atomic_write(path(cfg, rec["job_id"]), json.dumps(rec, indent=2, sort_keys=True) + "\n")


def create(cfg, *, job_id: str, campaign: str, worker: str, attempt: int, seq: int,
           submitted_at: float, deadline_at: float, kind: str = "gate",
           tier: str | None = None, commit: str | None = None, head: str | None = None,
           tree: str | None = None) -> dict:
    """`tier` is the gate run's tier (`loop`, `close`, ...): only a `loop` run's record is ever
    adopted (`live_gate`). `commit` is the header's commit under test, `head` the worktree's
    HEAD and `tree` the commit's tree at submit: adoption needs the worktree to match."""
    rec = {"schema": SCHEMA, "job_id": job_id, "kind": kind, "campaign": campaign,
           "worker": worker, "attempt": attempt, "seq_or_run": seq, "cursor": 0,
           "state": "submitted", "submitted_at": iso(submitted_at),
           "deadline_at": iso(deadline_at), "last_seen_at": iso(submitted_at),
           "excluded_workers": [], "tier": tier, "commit": commit, "head": head,
           "tree": tree}
    with _lock(cfg):
        _write(cfg, rec)
    return rec


def update(cfg, job_id: str, **fields) -> dict | None:
    """Merge `fields` into the record under the lock. A record already final keeps its state
    (only `state` in `FINAL` may replace a final one); returns the record as written, or None
    when there is none."""
    with _lock(cfg):
        rec = read(cfg, job_id)
        if rec is None:
            return None
        if rec.get("state") in FINAL and fields.get("state") not in FINAL:
            fields.pop("state", None)
        rec.update(fields)
        _write(cfg, rec)
        return rec


def _all(cfg) -> list[dict]:
    d = jobs_dir(cfg)
    if not d.is_dir():
        return []
    return [rec for p in d.glob("*.json") if (rec := read(cfg, p.stem)) is not None]


def max_seq(cfg, campaign: str) -> int:
    """The highest gate seq of any `<campaign>-gate-*` record, final ones included (0: none).
    `gates.next_gate_seq` allocates past it, so a job id the worker may still hold (a job
    cancelled before its first frame wrote a `gates/` file) is never submitted again."""
    d = jobs_dir(cfg)
    if not d.is_dir():
        return 0
    seqs = [int(m.group("seq")) for p in d.glob("*.json")
            if (m := _GATE_ID.match(p.stem)) and m.group("cid") == campaign]
    return max(seqs, default=0)


def live(cfg, campaign: str | None = None) -> list[dict]:
    """Records with a non-final state (of `campaign`, when given), lowest seq/attempt first."""
    out = []
    for rec in _all(cfg):
        if rec.get("state") not in LIVE:
            continue
        if campaign is not None and rec.get("campaign") != campaign:
            continue
        out.append(rec)
    return sorted(out, key=lambda r: (int(r.get("seq_or_run") or 0), int(r.get("attempt") or 0)))


def live_gate(cfg, campaign: str) -> dict | None:
    """The campaign's live gate record: the non-final `<campaign>-gate-*` one with the highest
    seq and attempt, else None. Only a record of the loop's gate step (`tier: loop`) counts: a
    close re-gate's or a debug run's record is never adopted by it."""
    recs = [r for r in live(cfg, campaign) if r.get("kind") == "gate"
            and r.get("tier") == "loop"
            and (m := _GATE_ID.match(str(r.get("job_id")))) and m.group("cid") == campaign]
    return recs[-1] if recs else None


def mark_done(cfg, job_id: str) -> None:
    """The job's `gate` event is written: a live record goes `done` (a final one, such as a
    `cancelled` record whose interrupted run was logged, keeps its state)."""
    with _lock(cfg):
        rec = read(cfg, job_id)
        if rec is not None and rec.get("state") in LIVE:
            rec["state"] = "done"
            _write(cfg, rec)
