"""`nw campaign runs|run` data (SPEC-0199): one record per persona run, joined from the ledger's
`run.start` / `run.end` and the run directory (`run.json`, `result.json`, `stderr.log`). The
ledger is authoritative for every field it carries, so a run reads the same with or without
its `run.json`; `run.json` only fills what the ledger lacks."""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import store
from .events import _is_transient

START_KEYS = ("persona", "model", "effort", "max_budget_usd", "timeout_s", "permission_mode")
END_KEYS = ("exit_code", "is_error", "timed_out", "interrupted", "duration_s", "cost_usd",
            "charged_usd", "usage", "session_id", "verdict_ok", "verdict", "result_tail")
RECORD_KEYS = (("campaign", "run_id") + START_KEYS + ("started_at",) + END_KEYS
               + ("runner_started_at", "source", "status", "verdict_status", "transient"))
STDERR_TAIL_LINES = 40
_PATH_FILES = (("prompt", "prompt.md"), ("raw", "result.json"), ("stderr", "stderr.log"),
               ("run_json", "run.json"), ("stream", "stream.jsonl"))     # stream: SPEC-0362


def verdict_status(verdict, verdict_ok) -> str:
    """The one-word reading of a verdict: its status / disposition / verdict field."""
    if verdict is None:
        return "—"
    if not isinstance(verdict, dict) or verdict_ok is False:
        return "invalid"
    for k in ("status", "disposition", "verdict"):
        if verdict.get(k) is not None:
            return str(verdict[k])
    return "?"


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _run_dirs(cfg, cid) -> dict[str, Path]:
    root = store.runs_dir(cfg, cid)
    if not root.is_dir():
        return {}
    return {p.name: p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")}


def _record(cid, run_id, start, end, run_json) -> dict:
    """Join one run's sources into the list-row record."""
    rj = run_json if isinstance(run_json, dict) else None
    rec: dict = {"campaign": cid, "run_id": run_id}
    for k in START_KEYS:
        rec[k] = start[k] if start and k in start else (rj or {}).get(k)
    rec["started_at"] = start.get("ts") if start else (rj or {}).get("started_at")
    for k in END_KEYS:
        rec[k] = end[k] if end and k in end else (rj or {}).get(k)
    rec["runner_started_at"] = (rj or {}).get("started_at")
    has_ledger = bool(start or end)
    rec["source"] = (("ledger+run.json" if rj else "ledger") if has_ledger
                     else "run.json" if rj else "files")
    rec["status"] = ("running" if start and not end and not rj
                     else "orphan" if not has_ledger and not rj else "done")
    rec["verdict_status"] = verdict_status(rec["verdict"], rec["verdict_ok"])
    rec["transient"] = bool(
        (rec["is_error"] or rec["timed_out"])
        and _is_transient(rec["result_tail"] or ("timed out" if rec["timed_out"] else "error")))
    return {k: rec[k] for k in RECORD_KEYS}


def _gather(cfg, cid, events=None) -> dict[str, dict]:
    store.read_state(cfg, cid)                      # unknown campaign → ValueError
    starts: dict[str, dict] = {}
    ends: dict[str, dict] = {}
    # SPEC-0346: export passes the ledger it already read, so the run join and the worker
    # map see one immutable set. None re-reads (the `nw campaign runs` path).
    if events is None:
        evs = store.read_ledger(cfg, cid, types=["run.start", "run.end"])
    else:
        evs = [e for e in events if e.get("type") in ("run.start", "run.end")]
    for ev in evs:
        rid = ev.get("run_id")
        if rid:
            (starts if ev["type"] == "run.start" else ends)[rid] = ev
    dirs = _run_dirs(cfg, cid)
    out = {}
    for rid in sorted(set(starts) | set(ends) | set(dirs)):
        d = dirs.get(rid)
        rj = _read_json(d / "run.json") if d else None
        out[rid] = _record(cid, rid, starts.get(rid), ends.get(rid), rj)
    return out


def campaign_runs(cfg, cid, events=None) -> list[dict]:
    """Every run of the campaign known to the ledger or the `runs/` directory, by run_id.
    `events` is an already-read ledger (SPEC-0346); without it the ledger is read here."""
    return list(_gather(cfg, cid, events).values())


def _resolve(runs: dict[str, dict], cid, run) -> str:
    if run in runs:
        return run
    hits = [r for r in runs if r.startswith(run)]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise ValueError(f"run prefix {run!r} in {cid} is ambiguous: {', '.join(hits)}")
    raise ValueError(f"no run {run!r} in {cid} (runs: {', '.join(runs) or 'none'})")


def campaign_run(cfg, cid, run) -> dict:
    """One run's full record: the list row plus file paths, tails and verdict lists."""
    runs = _gather(cfg, cid)
    rid = _resolve(runs, cid, run)
    rec = dict(runs[rid])
    d = store.runs_dir(cfg, cid) / rid
    paths: dict[str, str] = {}
    if d.is_dir():
        paths["dir"] = os.path.abspath(d)
        for key, name in _PATH_FILES:
            if (d / name).is_file():
                paths[key] = os.path.abspath(d / name)
    rec["paths"] = paths
    try:
        lines = (d / "stderr.log").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    rec["stderr_tail"] = lines[-STDERR_TAIL_LINES:]
    raw = _read_json(d / "result.json") if d.is_dir() else None
    denials = raw.get("permission_denials") if isinstance(raw, dict) else None
    rec["permission_denials"] = denials if isinstance(denials, list) else []
    verdict = rec["verdict"] if isinstance(rec["verdict"], dict) else {}
    for k in ("must_fix", "blockers"):
        v = verdict.get(k)
        rec[k] = v if isinstance(v, list) else []
    return rec
