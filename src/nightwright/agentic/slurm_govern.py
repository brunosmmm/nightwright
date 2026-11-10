"""Governing nw's Slurm jobs on the worker (SPEC-0512): `nw worker slurm-jobs` lists them,
`nw worker slurm-ctl VERB JOB [ARG]` holds, releases, tops, requeues, re-nices or re-classes one.

Only this worker's own jobs: a Slurm job is governed when its user is the calling user (on the
box: `nw`, invariant 6 of SPEC-0501) and its name is the nw job id of a job dir here whose
`job.json` records that Slurm job id. A worker whose executor is not `slurm` answers
`executor is local` (the coordinator's `nw queue jobs` reports it). The coordinator resolves
campaign / `GATEJOB-` ids to an nw job id from the list; this module takes an nw job id or a
numeric Slurm id."""
from __future__ import annotations

import getpass
import json
import re
import time
from pathlib import Path

from . import executor_slurm as ES
from . import executors as EX
from . import runproc
from . import worker as W

LIST_SCHEMA = "nw.worker.slurm_jobs.v1"
CTL_SCHEMA = "nw.worker.slurm_ctl.v1"
JOB_VERBS = ("hold", "release", "top", "requeue", "cancel")   # `slurm-ctl VERB JOB`
VERBS = JOB_VERBS + ("nice", "class")                   # + `nice JOB N`, `class JOB CLASS`
NICE_MAX = 10000
EXIT_REFUSED = 1            # unknown job, not ours, executor not slurm, bad argument
EXIT_SLURM = 3              # Slurm refused or could not be asked
# a transient "Resource temporarily unavailable" is retried this long (inside the coordinator's
# 60 s call timeout), every EAGAIN_SLEEP_S
EAGAIN_RETRY_S = 40.0
EAGAIN_SLEEP_S = 2.0
_EAGAIN_RE = re.compile(r"resource temporarily unavailable", re.I)


class Refused(Exception):
    def __init__(self, error: str, code: int = EXIT_REFUSED):
        super().__init__(error)
        self.error, self.code = error, code


def _me() -> str:
    return getpass.getuser()


def executor_refusal(cfg) -> str | None:
    """`executor is <name>` unless this host's config opts into Slurm (SPEC-0512)."""
    name = EX.configured(cfg)
    return None if name == ES.NAME else f"executor is {name}"


def _dirs(cfg) -> dict[int, tuple[Path, dict]]:
    """`{slurm job id: (job dir, job.json)}` of this worker's job dirs that went to Slurm."""
    out = {}
    jd = W.jobs_dir(cfg)
    for job in sorted(jd.iterdir()) if jd.is_dir() else []:
        rec = runproc.read_json(job / "job.json") if job.is_dir() else None
        jid = (rec or {}).get("slurm_job_id")
        if isinstance(jid, int) and not isinstance(jid, bool):
            out[jid] = (job, rec)
    return out


def _squeue(sett: dict, me: str) -> list[dict]:
    p = ES._run(sett, "squeue", "--json", "-u", me)
    jobs = ES._jobs_of(p) if p.returncode == 0 else None
    if jobs is None:
        raise ES.SlurmUnavailable(f"squeue: {ES._tail(p)}")
    return jobs


def _class_of(sett: dict, rec: dict, partition) -> str | None:
    cls = rec.get("job_class")
    if cls in ES.CLASSES:
        return cls
    for c in ES.CLASSES:                       # submitted before SPEC-0511: by its partition
        if partition and ES.target(sett, c)["partition"] == partition:
            return c
    return None


def _row(sett: dict, j: dict, job: Path, rec: dict, now: float) -> dict:
    f = ES._job_fields(j, "squeue")
    submit, start = ES._num(j.get("submit_time")), ES._num(j.get("start_time"))
    running = f["state"] in ES.RUNNING
    return {"job_id": job.name, "slurm_job_id": j.get("job_id"),
            "class": _class_of(sett, rec, j.get("partition")),
            "partition": j.get("partition") or None, "qos": j.get("qos") or None,
            "state": f["state"], "reason": f["reason"] if f["reason"] != "None" else None,
            "cpus": f["cpus"] or ES._num(j.get("cpus")), "mem_mb": f["mem_mb"],
            "node": j.get("nodes") or None, "nice": j.get("nice"),
            "held": bool(j.get("hold")), "restarts": j.get("restart_cnt") or 0,
            "waited_s": round(max(0.0, (start if running and start else now) - submit), 1)
            if submit else None,
            "ran_s": round(max(0.0, now - start), 1) if running and start else None,
            "campaign": rec.get("campaign"), "kind": rec.get("kind") or None,
            "warnings": rec.get("slurm_warnings") or []}


def rows(cfg, now: float | None = None) -> list[dict]:
    """The live (pending or running) Slurm jobs of this user that are nw jobs of this worker."""
    sett = ES.settings(cfg)
    me = _me()
    now = time.time() if now is None else now
    dirs = _dirs(cfg)
    out = []
    for j in _squeue(sett, me):
        hit = dirs.get(j.get("job_id"))
        if hit is None or j.get("user_name") != me or j.get("name") != hit[0].name:
            continue
        out.append(_row(sett, j, hit[0], hit[1], now))
    return sorted(out, key=lambda r: r["slurm_job_id"] or 0)


def listing(cfg) -> tuple[int, dict]:
    """`nw worker slurm-jobs --json`: `(exit code, nw.worker.slurm_jobs.v1)`."""
    why = executor_refusal(cfg)
    if why is not None:
        return EXIT_REFUSED, {"schema": LIST_SCHEMA, "ok": False, "error": why, "jobs": []}
    try:
        got = rows(cfg)
    except ES.SlurmUnavailable as e:
        return EXIT_SLURM, {"schema": LIST_SCHEMA, "ok": False, "error": str(e), "jobs": []}
    return 0, {"schema": LIST_SCHEMA, "ok": True, "user": _me(), "jobs": got}


def resolve(cfg, sett: dict, job: str) -> tuple[Path, int, dict]:
    """`(job dir, Slurm job id, Slurm fields)` for `job` (an nw job id or a numeric Slurm id),
    or `Refused`: unknown here, not this user's, or a Slurm job whose name is not the dir's."""
    dirs = _dirs(cfg)
    if job.isdigit():
        hit = dirs.get(int(job))
        if hit is None:
            raise Refused(f"Slurm job {job} is not an nw job of this worker")
        jdir, jid = hit[0], int(job)
    else:
        jdir = W.jobs_dir(cfg) / job
        rec = runproc.read_json(jdir / "job.json") if W.KEY_RE.match(job) else None
        jid = (rec or {}).get("slurm_job_id")
        if not isinstance(jid, int) or isinstance(jid, bool):
            raise Refused(f"no Slurm job for nw job {job} on this worker")
    p = ES._run(sett, "squeue", "--json", "-j", jid)
    if p.returncode != 0 and not ES._INVALID_RE.search(p.stderr or ""):
        raise ES.SlurmUnavailable(f"squeue: {ES._tail(p)}")
    jobs = ES._jobs_of(p) if p.returncode == 0 else []
    raw = next((j for j in jobs or [] if j.get("job_id") == jid), None)
    if raw is None:
        raise Refused(f"Slurm job {jid} ({jdir.name}) is not pending or running")
    if raw.get("user_name") != _me() or raw.get("name") != jdir.name:
        raise Refused(f"Slurm job {jid} is not an nw job (user {raw.get('user_name')!r}, "
                      f"name {raw.get('name')!r})")
    return jdir, jid, ES._job_fields(raw, "squeue")


def _argv(sett: dict, verb: str, jid: int, arg) -> list[str]:
    if verb in JOB_VERBS:
        return [verb, str(jid)]
    if verb == "nice":
        return ["update", f"JobId={jid}", f"Nice={int(arg)}"]
    t = ES.target(sett, arg)                    # class: the class's partition (and QOS)
    if not t["partition"] and not t["qos"]:
        raise Refused(f"class {arg} maps to no partition and no QOS here")
    return ["update", f"JobId={jid}",
            *([f"Partition={t['partition']}"] if t["partition"] else []),
            *([f"QOS={t['qos']}"] if t["qos"] else [])]


def _scontrol(sett: dict, argv: list[str]):
    """`scontrol ARGV`, retried while Slurm answers "Resource temporarily unavailable": on the
    box (26.05) a user's `update JobId=N Partition=...` is refused that way for up to ~20 s after
    the job was submitted or last updated (seen in SPEC-0512's real run), then accepted."""
    end = time.monotonic() + EAGAIN_RETRY_S
    while True:
        p = ES._run(sett, "scontrol", *argv)
        if p.returncode == 0 or not _EAGAIN_RE.search(p.stderr or p.stdout or "") \
                or time.monotonic() >= end:
            return p
        time.sleep(EAGAIN_SLEEP_S)


def control(cfg, verb: str, job: str, arg=None) -> tuple[int, dict]:
    """`nw worker slurm-ctl VERB JOB [ARG]`: `(exit code, nw.worker.slurm_ctl.v1)`."""
    out = {"schema": CTL_SCHEMA, "ok": False, "verb": verb, "job": job, "job_id": None,
           "slurm_job_id": None}
    try:
        why = executor_refusal(cfg)
        if why is not None:
            raise Refused(why)
        if verb not in VERBS:
            raise Refused(f"unknown verb {verb!r}")
        if verb == "nice" and not (isinstance(arg, int) and 0 <= arg <= NICE_MAX):
            raise Refused(f"nice must be an int from 0 to {NICE_MAX}")
        if verb == "class" and arg not in ES.CLASSES:
            raise Refused(f"class must be one of {', '.join(ES.CLASSES)}")
        sett = ES.settings(cfg)
        jdir, jid, info = resolve(cfg, sett, job)
        out.update(job_id=jdir.name, slurm_job_id=jid, state=info["state"])
        if verb == "class" and info["state"] not in ES.PENDING:
            raise Refused(f"{jdir.name} is {info['state']}: only a pending job changes class")
        if verb == "cancel":
            # a Slurm job nw already finished (a requeue after its run ended `done`
            # interrupted, SPEC-0510's TERM-as-cancel): `nw worker cancel` is a no-op for it,
            # so end it in Slurm here; the cancel file keeps any rerun from gating
            (jdir / "cancel").write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z") + "\n",
                                         encoding="utf-8")
            out["argv"] = ["scancel", str(jid)]
            p = ES._run(sett, "scancel", jid)
            if p.returncode != 0:
                raise Refused(f"scancel: {ES._tail(p)}", EXIT_SLURM)
            ES.forget(jid)
            out["ok"] = True
            return 0, out
        argv = _argv(sett, verb, jid, arg)
        out["argv"] = ["scontrol", *argv]
        p = _scontrol(sett, argv)
        if p.returncode != 0:
            raise Refused(f"scontrol {verb}: {ES._tail(p)}", EXIT_SLURM)
        if verb == "class":
            from . import worker_job as WJ
            t = ES.target(sett, arg)
            with W.jobs_lock(cfg):
                WJ._set_job_json(jdir, job_class=arg, slurm_partition=t["partition"],
                                 slurm_qos=t["qos"])
        ES.forget(jid)
        out["ok"] = True
        return 0, out
    except Refused as e:
        out["error"] = e.error
        return e.code, out
    except ES.SlurmUnavailable as e:
        out["error"] = str(e)
        return EXIT_SLURM, out


def dumps(rec: dict) -> str:
    return json.dumps(rec, ensure_ascii=False)
