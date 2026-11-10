"""`nw queue jobs` (SPEC-0512): the coordinator's view of nw's Slurm jobs and the verbs that
govern them, over the worker's forced-command SSH (`nw worker slurm-jobs` / `slurm-ctl` /
`cancel`).

A `JOB` operand is an nw job id, a campaign id (its live gate or persona job), a `GATEJOB-NNNN`
(its gate job's worker job) or a numeric Slurm id. It is resolved against the workers' live
lists: an exact nw id or Slurm id, else the one live job whose nw id starts with `<JOB>-` (more
than one is ambiguous: the caller names the nw id). The worker checks again that the Slurm job
is its own user's and an nw job (`slurm_govern.resolve`). Each verb that succeeds appends a
`queue_job_governed` event, so the conductor sees a human's reprioritization."""
from __future__ import annotations

import getpass
import os
import signal

from . import events as E
from . import remote_gates as RG
from . import workers as WK

SCHEMA = "nw.queue.jobs.v1"
LIST_SCHEMA = "nw.worker.slurm_jobs.v1"
CTL_SCHEMA = "nw.worker.slurm_ctl.v1"
VERBS = ("hold", "release", "top", "bump", "class", "requeue", "cancel")
CLASSES = ("campaign", "gatejob", "persona", "bulk")
BUMP_DOWN_DEFAULT = 1000
BUMP_DOWN_MAX = 10000
EVENT = "queue_job_governed"
CALL_TIMEOUT_S = 60.0


class GovernError(Exception):
    """A verb could not be carried out (unknown job, ambiguous, refused, unreachable)."""


def slurm_workers(cfg, name: str | None = None) -> list[dict]:
    """`[name]` when given (any SSH worker, so its own answer says whether it runs Slurm), else
    every SSH worker whose cached `info` reports `executor:slurm` (`workers.runs_slurm`)."""
    recs = WK.load(cfg)
    if name is not None:
        rec = next((r for r in recs if r["name"] == name), None)
        if rec is None:
            raise GovernError(f"no worker named {name!r} (nw workers list)")
        if rec.get("mode") == "pull":
            raise GovernError(f"worker {name} is a pull worker: nothing reaches it over SSH")
        return [rec]
    return [r for r in recs if r.get("mode") != "pull" and WK.runs_slurm(cfg, r["name"])]


def _schema_line(lines: list[dict], schema: str) -> dict | None:
    return next((r for r in lines if r.get("schema") == schema), None)


def _tail(err) -> str:
    tail = (err or b"").decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
    return tail[0]


def worker_jobs(w: dict) -> tuple[list[dict], str | None]:
    """`(rows, error)` of `nw worker slurm-jobs --json` on `w`; each row gets `worker`."""
    got = RG._call(w, "slurm-jobs", "--json", timeout=CALL_TIMEOUT_S)
    if got is None:
        return [], "could not reach the worker over SSH"
    rc, lines, err = got
    rec = _schema_line(lines, LIST_SCHEMA)
    if rec is None:
        return [], f"`nw worker slurm-jobs` failed (exit {rc}): {_tail(err)}".rstrip(": ")
    if not rec.get("ok"):
        return [], str(rec.get("error") or f"exit {rc}")
    return [{**r, "worker": w["name"]} for r in rec.get("jobs") or [] if isinstance(r, dict)], None


def listing(cfg, worker: str | None = None) -> dict:
    """`nw.queue.jobs.v1`: `{schema, workers: [{name, ok, error}], jobs: [...]}`."""
    out = {"schema": SCHEMA, "workers": [], "jobs": []}
    for w in slurm_workers(cfg, worker):
        rows, err = worker_jobs(w)
        out["workers"].append({"name": w["name"], "ok": err is None, "error": err})
        out["jobs"] += rows
    return out


def match(rows: list[dict], job: str) -> list[dict]:
    """The rows `job` names: a Slurm id, an exact nw id, else the nw ids under `<job>-`."""
    if job.isdigit():
        return [r for r in rows if r.get("slurm_job_id") == int(job)]
    exact = [r for r in rows if r.get("job_id") == job]
    return exact or [r for r in rows if str(r.get("job_id") or "").startswith(job + "-")]


def _gatejob_worker(cfg, job: str) -> str | None:
    if not job.startswith("GATEJOB-"):
        return None
    from . import gate_jobs as GJ
    try:
        rec = GJ.read(cfg, job)
    except (OSError, ValueError):
        return None
    return (rec or {}).get("worker") or None


def resolve(cfg, job: str, worker: str | None = None) -> tuple[dict, dict]:
    """`(worker record, row)` of the one live nw Slurm job `job` names, else GovernError."""
    worker = worker or _gatejob_worker(cfg, job)
    ws = slurm_workers(cfg, worker)
    if not ws:
        raise GovernError("no Slurm worker: no SSH worker's cached info reports executor:slurm "
                          "(nw workers check NAME; or pass --worker)")
    hits, errors = [], []
    for w in ws:
        rows, err = worker_jobs(w)
        if err:
            errors.append(f"{w['name']}: {err}")
        hits += [(w, r) for r in match(rows, job)]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise GovernError(f"{job} is ambiguous: " + ", ".join(
            f"{r['job_id']} (slurm {r['slurm_job_id']} on {w['name']})" for w, r in hits)
            + "; name one nw job id")
    raise GovernError(f"no live nw Slurm job matches {job}"
                      + (f" ({'; '.join(errors)})" if errors else ""))


def _by() -> str:
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return os.environ.get("USER") or "cli"


def _event(cfg, w: dict, row: dict, verb: str, by: str, **extra) -> dict | None:
    try:
        cid = row.get("campaign")
        real = isinstance(cid, str) and not cid.startswith(("GATEJOB-", "sentinel-"))
        return E.append(cfg, EVENT, campaign=cid if real else None, by=by,
                        data={"job": row["job_id"], "verb": verb, "by": by,
                              "slurm_id": row.get("slurm_job_id"), "worker": w["name"],
                              **extra})
    except (OSError, ValueError):
        return None                             # informational: the verb itself happened


def _ctl(w: dict, verb: str, job_id: str, *arg) -> dict:
    got = RG._call(w, "slurm-ctl", verb, job_id, *arg, timeout=CALL_TIMEOUT_S)
    if got is None:
        raise GovernError(f"could not reach {w['name']} over SSH")
    rc, lines, err = got
    rec = _schema_line(lines, CTL_SCHEMA)
    if rec is None:
        raise GovernError(f"`nw worker slurm-ctl` failed on {w['name']} (exit {rc}): "
                          f"{_tail(err)}".rstrip(": "))
    if not rec.get("ok"):
        raise GovernError(str(rec.get("error") or f"exit {rc}"))
    return rec


def _cancel(cfg, w: dict, row: dict, job: str) -> dict:
    """`nw worker cancel` (SPEC-0510: the cancel file + scancel) for an async job; a standalone
    `GATEJOB-` job runs as a sync `gate-job`, ended by stopping its coordinator process (as
    `nw gate cancel`), which closes the stream and the worker scancels it."""
    gid = job if job.startswith("GATEJOB-") else None
    if gid is None and str(row["job_id"]).startswith("GATEJOB-"):
        gid = str(row["job_id"]).split("-gate-", 1)[0]
    got = RG._call(w, "cancel", row["job_id"], timeout=CALL_TIMEOUT_S)
    if got is None:
        raise GovernError(f"could not reach {w['name']} over SSH")
    rc, lines, err = got
    st = next((r for r in lines if r.get("type") == "status"), {})
    if rc == 0 and st.get("state") in ("done", "error", "lost"):
        # nw finished this job but Slurm still has it queued (requeued after its run ended):
        # the worker's cancel was a no-op, so scancel it there (SPEC-0512 `slurm-ctl cancel`)
        _ctl(w, "cancel", row["job_id"])
        return {"via": "slurm-ctl cancel", "cancel": "scancel", "nw_state": st.get("state")}
    if rc == 0:
        return {"via": "worker cancel", "cancel": st.get("cancel") or "requested"}
    if gid is not None:
        from . import gate_jobs as GJ
        rec = GJ.read(cfg, gid) or {}
        if GJ.status_of(rec) in GJ.FINAL:
            raise GovernError(f"{gid} already ended: {GJ.status_of(rec)}")
        try:
            os.kill(int(rec["pid"]), signal.SIGTERM)
        except (OSError, TypeError, ValueError, KeyError) as e:
            raise GovernError(f"cannot signal {gid}'s process {rec.get('pid')}: {e}")
        return {"via": "gate job process", "pid": rec["pid"]}
    raise GovernError(f"`nw worker cancel {row['job_id']}` refused (exit {rc}): "
                      f"{_tail(err) or st.get('state')}; a sync gate job (sentinel, cadence) "
                      f"ends with its runner")


def govern(cfg, verb: str, job: str, *, worker: str | None = None, down: int | None = None,
           cls: str | None = None, by: str | None = None) -> dict:
    """Carry out one verb on the job `job` names; the result record (and the event)."""
    if verb not in VERBS:
        raise GovernError(f"unknown verb {verb!r}")
    if verb == "class" and cls not in CLASSES:
        raise GovernError(f"class must be one of {', '.join(CLASSES)}")
    if verb == "bump":
        down = BUMP_DOWN_DEFAULT if down is None else down
        if not 0 <= down <= BUMP_DOWN_MAX:
            raise GovernError(f"--down must be from 0 to {BUMP_DOWN_MAX} (raising a job's "
                              "priority needs a Slurm operator: Phase 2, slurmdbd)")
    w, row = resolve(cfg, job, worker)
    by = by or _by()
    extra = {}
    if verb == "cancel":
        extra = _cancel(cfg, w, row, job)
    elif verb == "bump":
        _ctl(w, "nice", row["job_id"], str(down))
        extra = {"nice": down}
    elif verb == "class":
        _ctl(w, "class", row["job_id"], cls)
        extra = {"class": cls, "from_class": row.get("class")}
    else:
        _ctl(w, verb, row["job_id"])
    ev = _event(cfg, w, row, verb, by, **extra)
    return {"ok": True, "verb": verb, "job": row["job_id"], "slurm_id": row.get("slurm_job_id"),
            "worker": w["name"], "by": by, "event_id": (ev or {}).get("id"), **extra}
