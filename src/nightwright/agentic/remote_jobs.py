"""Durable job records for async remote gate jobs (SPEC-0341): `<agentic_dir>/remote-jobs/<job_id>.json`.

A record says which worker runs the job, its attempt, the gate seq, the last applied frame `n`
(`cursor`), its state and its deadline. It outlives the coordinator, so a restarted queue finds
the campaign's live record before it allocates a new gate seq and adopts the job instead of
re-running it. Writes are atomic and serialized by one flock; a final state is never
overwritten by a non-final one, and `cancelled` by nothing (a cancel landing while the step
still attaches or gives up wins)."""
from __future__ import annotations

import datetime as dt
import fcntl
import glob
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
DEFAULT_MAX_REMOTE_ATTEMPTS = 2     # `agentic.workers.max_remote_attempts` (SPEC-0342)
# `{seq:03d}`: at least three digits, four from seq 1000 on
_GATE_ID = re.compile(r"^(?P<cid>.+)-gate-(?P<seq>[0-9]{3,})-a(?P<attempt>[0-9]+)\Z")
# SPEC-0344: `<campaign>-persona-<run_id>-a<attempt>`
_PERSONA_ID = re.compile(r"^(?P<cid>.+)-persona-(?P<run>[0-9]{3,}-[^/]+)-a(?P<attempt>[0-9]+)\Z")


def jobs_dir(cfg) -> Path:
    return agentic_dir(cfg) / "remote-jobs"


def path(cfg, job_id: str) -> Path:
    return jobs_dir(cfg) / f"{job_id}.json"


def gate_job_id(campaign_id: str, seq: int, attempt: int = 1) -> str:
    return f"{campaign_id}-gate-{seq:03d}-a{attempt}"


def settings(cfg) -> dict:
    """`{worker_ttl_s, job_lease_s, max_remote_attempts}` from `agentic.workers`. For the first
    two, anything but a positive finite number falls back to the default. SPEC-0342:
    `max_remote_attempts` (absent: 2) must be an integer of at least 1 (`bool` excluded);
    any other value raises `ValueError` naming the key, a config error the gate step reports."""
    raw = (cfg.get("agentic") or {}).get("workers")
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for key, default in (("worker_ttl_s", DEFAULT_WORKER_TTL_S),
                         ("job_lease_s", DEFAULT_JOB_LEASE_S)):
        v = raw.get(key)
        ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) \
            and v > 0
        out[key] = float(v) if ok else default
    v = raw.get("max_remote_attempts", DEFAULT_MAX_REMOTE_ATTEMPTS)
    if not isinstance(v, int) or isinstance(v, bool) or v < 1:
        raise ValueError(f"config error: agentic.workers.max_remote_attempts must be an "
                         f"integer of at least 1 (got {v!r})")
    out["max_remote_attempts"] = v
    return out


def has_async(worker: dict) -> bool:
    """The worker's cached `info` (written by `nw workers check`) lists the `async` feature. No
    cached `info` reads as an old worker. A pull worker (SPEC-0334) is always async."""
    if worker.get("mode") == "pull":
        return True
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
           tree: str | None = None, excluded_workers=()) -> dict:
    """`tier` is the gate run's tier (`loop`, `close`, ...): only a `loop` run's record is ever
    adopted (`live_gate`). `commit` is the header's commit under test, `head` the worktree's
    HEAD and `tree` the commit's tree at submit: adoption needs the worktree to match.
    SPEC-0342: `excluded_workers` are the workers earlier attempts of the job were lost on."""
    rec = {"schema": SCHEMA, "job_id": job_id, "kind": kind, "campaign": campaign,
           "worker": worker, "attempt": attempt, "seq_or_run": seq, "cursor": 0,
           "state": "submitted", "submitted_at": iso(submitted_at),
           "deadline_at": iso(deadline_at), "last_seen_at": iso(submitted_at),
           "excluded_workers": list(excluded_workers), "tier": tier, "commit": commit,
           "head": head, "tree": tree}
    with _lock(cfg):
        _write(cfg, rec)
    return rec


def update(cfg, job_id: str, **fields) -> dict | None:
    """Merge `fields` into the record under the lock. A record already final keeps its state
    (only `state` in `FINAL` may replace a final one, and nothing replaces `cancelled`: an
    abandon or a park is never undone by a racing give-up); returns the record as written, or
    None when there is none."""
    with _lock(cfg):
        rec = read(cfg, job_id)
        if rec is None:
            return None
        if rec.get("state") == "cancelled" or (rec.get("state") in FINAL
                                               and fields.get("state") not in FINAL):
            fields.pop("state", None)
        rec.update(fields)
        _write(cfg, rec)
        return rec


def _all(cfg, campaign: str | None = None) -> list[dict]:
    """Every record (of `campaign`, when given: only its `<campaign>-*` files are parsed)."""
    d = jobs_dir(cfg)
    if not d.is_dir():
        return []
    pat = f"{glob.escape(campaign)}-*.json" if campaign is not None else "*.json"
    return [rec for p in d.glob(pat) if (rec := read(cfg, p.stem)) is not None]


def campaigns(cfg) -> set[str]:
    """The campaigns that have gate or persona (SPEC-0344) job records, read from the file
    names (nothing parsed)."""
    d = jobs_dir(cfg)
    if not d.is_dir():
        return set()
    return {m.group("cid") for p in d.glob("*.json")
            if (m := _GATE_ID.match(p.stem) or _PERSONA_ID.match(p.stem))}


def prune(cfg, campaign: str, keep=()) -> list[str]:
    """Remove the campaign's final records (not those in `keep`): for a campaign that has
    ended, which never gates again, so `max_seq` no longer needs them. Returns the job ids."""
    out = []
    with _lock(cfg):
        for rec in _all(cfg, campaign):
            if rec.get("campaign") == campaign and rec.get("state") in FINAL \
                    and rec["job_id"] not in keep:
                path(cfg, rec["job_id"]).unlink(missing_ok=True)
                if rec.get("pull"):             # SPEC-0334: its spool, lock and uploads
                    up = agentic_dir(cfg) / "job-uploads"
                    for p in (agentic_dir(cfg) / "job-frames" / f"{rec['job_id']}.jsonl",
                              agentic_dir(cfg) / "job-frames" / f"{rec['job_id']}.lock",
                              up / f"{rec['job_id']}.result.bundle",
                              *(up.glob(f"{rec['job_id']}.frame-*") if up.is_dir() else ())):
                        p.unlink(missing_ok=True)
                out.append(rec["job_id"])
    return out


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
    for rec in _all(cfg, campaign):
        if rec.get("state") not in LIVE:
            continue
        if campaign is not None and rec.get("campaign") != campaign:
            continue
        out.append(rec)
    return sorted(out, key=lambda r: (_int0(r.get("seq_or_run")), _int0(r.get("attempt"))))


def _int0(v) -> int:
    """`v` as an int for sorting; 0 for a missing or malformed value (a list, a dict, a
    non-numeric string), so one bad record never breaks `live` for every caller."""
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def live_gate(cfg, campaign: str) -> dict | None:
    """The campaign's live gate record: the non-final `<campaign>-gate-*` one with the highest
    seq and attempt, else None. Only a record of the loop's gate step (`tier: loop`) counts: a
    close re-gate's or a debug run's record is never adopted by it."""
    recs = [r for r in live(cfg, campaign) if r.get("kind") == "gate"
            and r.get("tier") == "loop"
            and (m := _GATE_ID.match(str(r.get("job_id")))) and m.group("cid") == campaign]
    return recs[-1] if recs else None


def newest_gate(cfg, campaign: str) -> dict | None:
    """SPEC-0342: the campaign's newest loop gate record of any state (highest seq, then
    attempt), else None. A `lost` one is a give-up whose next attempt was never written."""
    recs = [r for r in _all(cfg, campaign) if r.get("kind") == "gate"
            and r.get("tier") == "loop" and r.get("campaign") == campaign
            and (m := _GATE_ID.match(str(r.get("job_id")))) and m.group("cid") == campaign]
    recs.sort(key=lambda r: (int(r.get("seq_or_run") or 0), int(r.get("attempt") or 0)))
    return recs[-1] if recs else None


def mark_done(cfg, job_id: str) -> None:
    """The job's `gate` event is written: a live record goes `done` (a final one, such as a
    `cancelled` record whose interrupted run was logged, keeps its state)."""
    with _lock(cfg):
        rec = read(cfg, job_id)
        if rec is not None and rec.get("state") in LIVE:
            rec["state"] = "done"
            _write(cfg, rec)
