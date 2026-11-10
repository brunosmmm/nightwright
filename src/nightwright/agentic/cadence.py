"""Cadence state: when a project's heavy test jobs are due (SPEC-0491).

`cadence:` in `.nw/project.yaml` lists named jobs with triggers (every N merges, every H hours,
a merge touching given paths, or every merge). This module is the data half: the per-project
state `<agentic>/cadence/<project>.json` (`nw.cadence.v1`), the merge counter the close hook
feeds, a pure `due` rule, the ensure / begin / finish / abort / reconcile accounting a runner
calls, and the read-only status rows behind `nw gate cadence status`. It starts no run
(SPEC-0493). An absent or empty `cadence` writes no file.

Every function that stamps takes `now` (epoch seconds, default the clock; the unit of
`sentinel._now`). Time stamps are timezone-aware ISO-8601 strings. All mutating functions hold
`<project>.json.lock`.
"""
from __future__ import annotations

import datetime as dt
import json
from contextlib import contextmanager
from pathlib import Path

from ..export import _atomic_write
from . import events as E
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
    out = []
    for j in jobs:
        row = rows.get(j["name"])
        base = {"name": j["name"], "triggers": P.cadence_triggers(j),
                "every_n_merges": j.get("every_n_merges")}
        if not isinstance(row, dict):
            r = {**base, "merges_since": 0, "paths_hit": [], "last_run_at": None,
                 "last_run_sha": None, "last_ok": None, "due": None, "next": "not seeded",
                 "running": "no"}
        else:
            reason = due(j, row, base_sha, t)
            r = {**base, "merges_since": row.get("merges_since", 0),
                 "paths_hit": list(row.get("paths_hit") or []),
                 "last_run_at": row.get("last_run_at"), "last_run_sha": row.get("last_run_sha"),
                 "last_ok": row.get("last_ok"), "due": reason,
                 "next": _next(j, row, base_sha, t, reason), "running": "no"}
        out.append({k: r[k] for k in STATUS_KEYS})
    return out
