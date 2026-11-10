"""Cadence state: when a project's heavy test jobs are due (SPEC-0491).

`cadence:` in `.nw/project.yaml` lists named jobs with triggers (every N merges, every H hours,
a merge touching given paths, or every merge). This module is the data half: the per-project
state `<agentic>/cadence/<project>.json` (`nw.cadence.v1`), the merge counter the close hook
feeds, a pure `due` rule, the ensure / begin / finish / abort / reconcile accounting a runner
calls, and the read-only status rows behind `nw gate cadence status`. SPEC-0493 adds `run` (one
job on the base under the sentinel run lock and a gate slot, settled through `redmain` with
source `cadence:<job>`) and `force`. An absent or empty `cadence` writes no file.

Every function that stamps takes `now` (epoch seconds, default the clock; the unit of
`sentinel._now`). Time stamps are timezone-aware ISO-8601 strings. All mutating functions hold
`<project>.json.lock`.
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
import time
from contextlib import contextmanager
from pathlib import Path

from ..export import _atomic_write
from . import events as E
from . import gate_slots
from . import gates as G
from . import project as P
from . import sentinel as SN
from . import store

SCHEMA = "nw.cadence.v1"
STATUS_SCHEMA = "nw.cadence.status.v1"
PATHS_CAP = 20
UNKNOWN_PATH = "*"
STATUS_KEYS = ("name", "triggers", "merges_since", "every_n_merges", "paths_hit", "last_run_at",
               "last_run_sha", "last_ok", "due", "next", "running")


def state_dir(cfg) -> Path:
    return store.agentic_dir(cfg) / "cadence"


def state_path(cfg, project) -> Path:
    return state_dir(cfg) / f"{E._safe(str(project))}.json"


@contextmanager
def locked(cfg, project):
    with E._locked(Path(f"{state_path(cfg, project)}.lock")):
        yield


def _iso(now) -> str:
    return SN._iso(now)


def _ts(value):
    return SN._ts(value)


def _jobs(cfg, project, project_cfg) -> list:
    pcfg = project_cfg if project_cfg is not None else P.project_config(cfg, project)
    return list(pcfg.get("cadence") or [])


def initial_row(now=None) -> dict:
    return {"merges_since": 0, "paths_hit": [], "seeded_at": _iso(now), "last_run_at": None,
            "last_run_sha": None, "last_ok": None, "last_green_at": None, "last_green_sha": None,
            "last_error": None, "forced": False}


def _fresh() -> dict:
    return {"schema": SCHEMA, "jobs": {}}


def _valid(doc) -> bool:
    return (isinstance(doc, dict) and doc.get("schema") == SCHEMA
            and isinstance(doc.get("jobs"), dict))


def _read_doc(path: Path):
    """The parsed document; None when missing; False when not a valid state file. OSError
    propagates."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        doc = json.loads(text)
    except ValueError:
        return False
    return doc if _valid(doc) else False


def _journal(cfg, project, **fields) -> None:
    from . import queue as Q
    Q.journal(cfg, "cadence_unreadable", project=project, **fields)


def _load_locked(cfg, project) -> dict:
    """Under the lock: the state, fresh when missing; a bad file is renamed aside and journalled."""
    path = state_path(cfg, project)
    doc = _read_doc(path)
    if doc is None:
        return _fresh()
    if doc is False:
        stamp = store.now_iso().replace(":", "")
        bad = path.with_name(f"{path.name}.bad-{stamp}")
        n = 1
        while bad.exists():                      # two bad files in one second keep both
            bad = path.with_name(f"{path.name}.bad-{stamp}-{n}")
            n += 1
        path.rename(bad)
        _journal(cfg, project, path=str(bad))
        return _fresh()
    return doc


def _write(cfg, project, doc) -> None:
    path = state_path(cfg, project)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps(doc, indent=2) + "\n")


def _row(doc, name, now) -> dict:
    return doc["jobs"].setdefault(name, initial_row(now))


def _ensure_locked(doc, names, now) -> bool:
    changed = False
    for n in names:
        if n not in doc["jobs"]:
            doc["jobs"][n] = initial_row(now)
            changed = True
    return changed


def _guarded(cfg, project, fn):
    """Run `fn`; an OSError is journalled as `cadence_unreadable` and swallowed (None)."""
    try:
        return fn()
    except OSError as e:
        _journal(cfg, project, error=str(e))
        return None


def ensure(cfg, project, *, project_cfg=None, now=None) -> None:
    """Create each missing job row; never reset one that exists. No cadence: no file."""
    jobs = _jobs(cfg, project, project_cfg)
    if not jobs:
        return

    def run():
        with locked(cfg, project):
            doc = _load_locked(cfg, project)
            if _ensure_locked(doc, [j["name"] for j in jobs], now) or not state_path(cfg, project).exists():
                _write(cfg, project, doc)
    _guarded(cfg, project, run)


def _cap(paths) -> list:
    return list(paths)[-PATHS_CAP:]


def record_merge(cfg, project, merged, paths, *, project_cfg=None, now=None) -> None:
    """One ff-merge landed (SPEC-0491 merge hook): `merges_since` +1 on every job and the
    matching paths (or `*` when `paths` is None) appended to `paths_hit`. A repeat of the last
    counted `merged` sha is a no-op. OSError propagates."""
    jobs = _jobs(cfg, project, project_cfg)
    if not jobs:
        return
    with locked(cfg, project):
        doc = _load_locked(cfg, project)
        _ensure_locked(doc, [j["name"] for j in jobs], now)
        if doc.get("last_merge_sha") == merged:
            _write(cfg, project, doc)
            return
        for j in jobs:
            row = doc["jobs"][j["name"]]
            row["merges_since"] = int(row.get("merges_since") or 0) + 1
            pats = j.get("on_paths") or []
            if not pats:
                continue
            if paths is None:
                hits = [UNKNOWN_PATH]
            else:
                hits = [p for p in paths if P.path_matches(pats, p)]
            row["paths_hit"] = _cap(list(row.get("paths_hit") or []) + hits)
        doc["last_merge_sha"] = merged
        _write(cfg, project, doc)


def due(job, st, base_sha, now) -> str | None:
    """The reason `job` (a config entry) is due given its state row `st`, or None (SPEC-0491)."""
    if st.get("forced"):
        return "forced"
    last_sha, ok = st.get("last_run_sha"), st.get("last_ok")
    if last_sha is not None and base_sha == last_sha:
        if ok is True:
            return None
        ran = _ts(st.get("last_run_at"))
        return "retry" if ran is None or now - ran >= SN.RETRY_S else None
    merges = int(st.get("merges_since") or 0)
    if job.get("always") is True and merges > 0:
        return "always"
    n = job.get("every_n_merges")
    if n is not None and merges >= n:
        return "merges"
    if st.get("paths_hit"):
        return "paths"
    h = job.get("every_hours")
    if h is not None:
        since = _ts(st.get("last_run_at") or st.get("seeded_at"))
        if since is not None and now - since >= h * 3600:
            return "hours"
    return None


def _consume(row, error, ok, sha, now) -> None:
    inf = row.pop("inflight", None) or {}
    row["merges_since"] = max(0, int(row.get("merges_since") or 0) - int(inf.get("merges") or 0))
    cur, taken = list(row.get("paths_hit") or []), list(inf.get("paths") or [])
    for k in range(min(len(cur), len(taken)), 0, -1):
        if cur[:k] == taken[-k:]:
            cur = cur[k:]
            break
    row["paths_hit"] = _cap(cur)
    row["last_run_at"] = _iso(now)
    row["last_run_sha"] = sha
    row["last_ok"] = ok
    if ok is True:
        row["last_green_at"], row["last_green_sha"], row["last_error"] = _iso(now), sha, None
    else:
        row["last_error"] = error


def _reconcile_locked(doc, now) -> bool:
    changed = False
    for row in doc["jobs"].values():
        inf = row.get("inflight")
        if inf is not None:
            _consume(row, "crashed", None, (inf or {}).get("sha"), now)
            changed = True
    return changed


def reconcile(cfg, project, *, now=None) -> None:
    """Treat each leftover `inflight` (a crash) as a failed finish."""
    def run():
        with locked(cfg, project):
            if state_path(cfg, project).exists():
                doc = _load_locked(cfg, project)
                if _reconcile_locked(doc, now):
                    _write(cfg, project, doc)
    _guarded(cfg, project, run)


def begin(cfg, project, job, sha, *, now=None) -> None:
    """A run of the job named `job` starts at `sha`: snapshot the counters, clear `forced`."""
    with locked(cfg, project):
        doc = _load_locked(cfg, project)
        _reconcile_locked(doc, now)
        row = _row(doc, job, now)
        row["inflight"] = {"merges": row.get("merges_since", 0),
                           "paths": list(row.get("paths_hit") or []), "sha": sha,
                           "forced": bool(row.get("forced"))}
        row["forced"] = False
        _write(cfg, project, doc)


def finish(cfg, project, job, ok, sha, error=None, *, now=None) -> None:
    """A run ended (any outcome but an interrupt). `ok` True green, False red, None failed."""
    with locked(cfg, project):
        doc = _load_locked(cfg, project)
        _consume(_row(doc, job, now), error, ok, sha, now)
        _write(cfg, project, doc)


def abort(cfg, project, job, *, now=None) -> None:
    """An interrupted run: drop `inflight`, restore `forced`; counters and `last_*` untouched."""
    with locked(cfg, project):
        doc = _load_locked(cfg, project)
        row = doc["jobs"].get(job)
        if row is None or "inflight" not in row:
            return
        row["forced"] = bool((row.pop("inflight") or {}).get("forced"))
        _write(cfg, project, doc)


def force(cfg, project, job, *, now=None) -> None:
    """Set the job's `forced` flag (`nw gate cadence run`, a red-main confirmation): `due`
    returns `forced` until a run of it begins. OSError propagates."""
    with locked(cfg, project):
        doc = _load_locked(cfg, project)
        _row(doc, job, now)["forced"] = True
        _write(cfg, project, doc)


# ---- the run (SPEC-0493) -------------------------------------------------------------------

FAILING_EVENT_MAX = 50


def pick(cfg, project, *, project_cfg=None, base_sha=None, now=None):
    """The runner scan for one project (SPEC-0493): seed the rows (`ensure`), consume a
    crash-left `inflight` (`reconcile`), then `(job_name, reason)` for the job to start, or
    None. A forced job wins (ties: list order); otherwise the due job with the oldest
    `last_run_at or seeded_at` (ties: list order). OSError propagates."""
    jobs = [j for j in _jobs(cfg, project, project_cfg) if isinstance(j, dict) and j.get("name")]
    if not jobs:
        return None
    now = time.time() if now is None else now
    ensure(cfg, project, project_cfg={"cadence": jobs}, now=now)
    reconcile(cfg, project, now=now)
    doc = _read_doc(state_path(cfg, project)) or _fresh()
    best = None
    for i, j in enumerate(jobs):
        row = doc["jobs"].get(j["name"]) or initial_row(now)
        why = due(j, row, base_sha, now)
        if why is None:
            continue
        if why == "forced":
            return j["name"], why
        since = _ts(row.get("last_run_at") or row.get("seeded_at"))
        key = (since if since is not None else float("-inf"), i)
        if best is None or key < best[0]:
            best = (key, j["name"], why)
    return (best[1], best[2]) if best else None


def campaign_id(project, job) -> str:
    """The run's campaign-shaped id (a `sentinel-` prefix: SPEC-0490 reads it as a sentinel run)."""
    return f"sentinel-{E._safe(str(project))}-{job}"


def _find_job(pcfg, job) -> dict:
    hit = next((j for j in pcfg.get("cadence") or []
                if isinstance(j, dict) and j.get("name") == job), None)
    if hit is None:
        raise ValueError(f"{pcfg.get('project') or '?'} has no cadence job {job!r}")
    return hit


def run(cfg, project, job, *, project_cfg=None, stop=None, now=None, reason=None) -> dict:
    """Run cadence job `job` of `project` once on the base HEAD (SPEC-0493): under the sentinel
    run lock (Busy before anything when held), in the sentinel worktree, holding one gate slot,
    on a worker when one is placeable and the header would be accepted, else locally. A red or
    green run is settled through `redmain` with source `cadence:<job>`; `ok` None (a failure, a
    `RemoteFailure`) calls neither hook; an interrupt calls `abort`. Returns
    `{project, job, ok, sha, interrupted, error, deferred, reason, failing, logs}`."""
    with SN.run_lock(cfg, project):
        return _run_locked(cfg, project, job, project_cfg=project_cfg, stop=stop, now=now,
                           reason=reason)


def _run_locked(cfg, project, job, *, project_cfg, stop, now, reason) -> dict:
    from . import redmain as RM
    from . import remote_gates as RG
    pcfg = project_cfg or P.project_config(cfg, project)
    spec = _find_job(pcfg, job)
    root = pcfg.get("_root") or str(P.project_root(cfg, project))
    base = SN._base(pcfg)
    wt = SN.worktree_path(cfg, project)
    sha = SN._prepare_worktree(root, wt, base, pcfg)
    begin(cfg, project, job, sha, now=now)
    started = time.monotonic()
    out = {"project": project, "job": job, "ok": None, "sha": sha, "interrupted": False,
           "error": None, "deferred": False, "reason": reason, "failing": [], "logs": []}
    owner = campaign_id(project, job)
    logs = wt.parent / f"{wt.name}.logs" / f"cadence-{job}"
    tmp = wt.parent / f"{wt.name}.tmp"
    gate = P.cadence_gate(spec)
    one = {**pcfg, "gates": [gate]}
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        seq = G.next_gate_seq_in(logs)
        pools, repool, state = SN._placement(cfg, project, one, wt, seq)
        state["id"] = owner
        if pools is not None and RG.header_refusal(
                {**one, "gates": P.gates_for(one, "close")}, state, cfg, seq) is not None:
            pools, repool = None, None
        sent_state = {"id": owner, "project": project, "repo": {"worktree": str(wt), "base": base}}
        env = {**G.tmp_env(tmp), "NW_DATA_DIR": str(cfg["data_dir"]),
               "NW_CONFIG_DIR": str(cfg.get("config_dir") or ""), "NW_CAMPAIGN": "",
               "NW_SPEC_ID": "", "NW_WORKTREE": str(wt), "NW_BASE": base,
               "NW_SENTINEL": str(project), **G.all_tiers_env(cfg, sent_state, header=True)}
        ph = {"spec_id": None, "campaign": owner, "worktree": str(wt), "base": base}
        results, interrupted, failure = [], False, None
        with gate_slots.acquire(cfg, owner, stop=stop, pools=pools, repool=repool) as hold:
            if hold.interrupted:
                interrupted = True
            elif hold.pool is not None:
                try:
                    r = RG.run_detached(hold.pool, one, state, cfg, seq, logs, stop=stop)
                except RG.RemoteFailure as f:
                    failure = f"remote cadence run on {hold.pool} failed ({f.stage}): {f.error}"
                else:
                    results = list(r.results) if r is not None else []
                    interrupted = r is None or r.interrupted
            elif stop is not None and stop.is_set():
                interrupted = True
            else:
                rel = f"{seq:03d}-{gate['name']}.log"
                results.append(G.run_gate_with_retries(gate, wt, env, logs / rel, ph,
                                                       log_rel=rel, stop=stop))
        shutil.rmtree(tmp, ignore_errors=True)
        interrupted = interrupted or any(r.interrupted for r in results)
    except BaseException as e:
        shutil.rmtree(tmp, ignore_errors=True)
        if isinstance(e, Exception):
            out["error"] = f"{type(e).__name__}: {e}"[:500]
            finish(cfg, project, job, None, sha, out["error"], now=now)
            _event(cfg, out, started)
            return out
        abort(cfg, project, job, now=now)
        raise
    if interrupted:
        abort(cfg, project, job, now=now)
        out["interrupted"] = True
        return out
    out["logs"] = [str(logs / r.log) for r in results if r.log]
    if failure is not None:
        out["error"] = failure[:500]
    elif not results:
        out["error"] = "the run produced no gate result"
    else:
        out["ok"] = all(r.ok for r in results if r.required)
        if not out["ok"]:
            out["failing"] = SN._failing(results)
    finish(cfg, project, job, out["ok"], sha, out["error"], now=now)
    if out["ok"] is not None and RM.enabled(cfg):
        source = f"cadence:{job}"
        try:
            if out["ok"]:
                RM.on_green(cfg, project, sha, now=now, source=source)
            else:
                red_logs = [str(logs / r.log) for r in results
                            if r.required and not r.ok and r.log]
                rec = RM.on_red(cfg, project, sha, list(out["failing"]), root=root, base=base,
                                logs=red_logs, now=now, source=source)
                out["deferred"] = RM.source_of(rec) != source
        except (OSError, ValueError) as e:      # best effort: the run result stands
            SN._journal(cfg, project, f"redmain.{'on_green' if out['ok'] else 'on_red'} "
                                      f"failed ({source}): {e}")
    _event(cfg, out, started)
    return out


def _event(cfg, out, started) -> None:
    data = {"project": out["project"], "job": out["job"], "ok": out["ok"], "sha": out["sha"],
            "reason": out["reason"], "duration_s": max(0, int(time.monotonic() - started))}
    if out["ok"] is False:
        data["failing"] = list(out["failing"])[:FAILING_EVENT_MAX]
    if out["error"]:
        data["error"] = out["error"]
    try:
        E.append(cfg, "cadence", project=out["project"], by="queue", data=data)
    except (ValueError, OSError):
        pass


# ---- status ------------------------------------------------------------------------------------

def _next(job, row, base_sha, now, reason) -> str | None:
    if reason is not None:
        return None
    if row.get("last_ok") is True and row.get("last_run_sha") == base_sha:
        return "when the base moves"
    merges = int(row.get("merges_since") or 0)
    if job.get("always") is True and merges == 0:
        return "on the next merge"
    n = job.get("every_n_merges")
    if n is not None and merges < n:
        return f"in {n - merges} merges"
    if job.get("on_paths") and not row.get("paths_hit"):
        return "on a matching path"
    waits = []
    h = job.get("every_hours")
    since = _ts(row.get("last_run_at") or row.get("seeded_at"))
    if h is not None and since is not None:
        waits.append(since + h * 3600)
    ran = _ts(row.get("last_run_at"))
    if row.get("last_run_sha") is not None and row.get("last_run_sha") == base_sha and ran is not None:
        waits.append(ran + SN.RETRY_S)           # red or failed: the retry backoff
    if waits:
        return "at " + store.now_iso(dt.datetime.fromtimestamp(min(waits)).astimezone())
    return None


def status_rows(cfg, project, *, project_cfg=None, base_sha=None, now=None) -> list[dict]:
    """One dict per configured job (keys `STATUS_KEYS`). Read-only: never seeds, never creates
    the file; a missing job row or file reads as unseeded."""
    jobs = _jobs(cfg, project, project_cfg)
    try:
        doc = _read_doc(state_path(cfg, project))
    except OSError as e:
        _journal(cfg, project, error=str(e))
        doc = None
    rows = (doc or {}).get("jobs") or {}
    t = SN._now(now)
    running = "yes" if jobs and SN.running(cfg, project) else "no"   # SPEC-0493
    out = []
    for j in jobs:
        row = rows.get(j["name"])
        base = {"name": j["name"], "triggers": P.cadence_triggers(j),
                "every_n_merges": j.get("every_n_merges")}
        if not isinstance(row, dict):
            r = {**base, "merges_since": 0, "paths_hit": [], "last_run_at": None,
                 "last_run_sha": None, "last_ok": None, "due": None, "next": "not seeded",
                 "running": running}
        else:
            reason = due(j, row, base_sha, t)
            r = {**base, "merges_since": row.get("merges_since", 0),
                 "paths_hit": list(row.get("paths_hit") or []),
                 "last_run_at": row.get("last_run_at"), "last_run_sha": row.get("last_run_sha"),
                 "last_ok": row.get("last_ok"), "due": reason,
                 "next": _next(j, row, base_sha, t, reason), "running": running}
        out.append({k: r[k] for k in STATUS_KEYS})
    return out
