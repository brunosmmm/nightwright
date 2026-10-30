"""SPEC-0293: a cross-campaign record of flaky tests, `<agentic_dir>/flaky-tests.json`.

Not a ledger: a derived, bounded summary. Per project and test id it keeps how often the test
flaked, when first and last, through which source, and the last campaigns. Entries unseen for
`EXPIRY_DAYS` are dropped on every write; a project keeps at most `PROJECT_CAP` entries. Writes
are one read-modify-write under `flaky.lock`; the file is replaced atomically."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from ..export import _atomic_write
from . import store

SCHEMA = "wt.flaky.v1"
SOURCES = ("rerun", "triage_retry")        # rerun: SPEC-0289 `flaky` ids; triage_retry: retry_gate
EXPIRY_DAYS = 90
PROJECT_CAP = 500
CAMPAIGNS_KEPT = 10


def path(cfg) -> Path:
    return store.agentic_dir(cfg) / "flaky-tests.json"


def _lock_path(cfg) -> Path:
    return store.agentic_dir(cfg) / "flaky.lock"


def _fresh() -> dict:
    return {"schema": SCHEMA, "projects": {}}


def _valid_entry(e) -> bool:
    return (isinstance(e, dict)
            and isinstance(e.get("count"), int) and not isinstance(e["count"], bool)
            and isinstance(e.get("first_seen"), str) and isinstance(e.get("last_seen"), str)
            and isinstance(e.get("sources"), dict) and isinstance(e.get("campaigns"), list)
            and all(isinstance(v, int) for v in e["sources"].values()))


def _valid(doc) -> bool:
    return (isinstance(doc, dict) and doc.get("schema") == SCHEMA
            and isinstance(doc.get("projects"), dict)
            and all(isinstance(p, dict) and all(_valid_entry(e) for e in p.values())
                    for p in doc["projects"].values()))


def _load(cfg, stamp: str) -> dict:
    """The record, a fresh one when absent; an unreadable or foreign file is moved aside to
    `flaky-tests.json.bad-<stamp>` first. A read error (OSError) is not a bad file: it propagates
    so the caller's best-effort note covers it."""
    p = path(cfg)
    if not p.is_file():
        return _fresh()
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:                  # incl. UnicodeDecodeError
        doc = None
    if _valid(doc):
        return doc
    p.replace(p.with_name(f"{p.name}.bad-{stamp}"))
    return _fresh()


def _parse(ts):
    try:
        return dt.datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def _prune(entries: dict, now: dt.datetime) -> dict:
    cutoff = now - dt.timedelta(days=EXPIRY_DAYS)
    kept = {}
    for tid, e in entries.items():
        seen = _parse(e.get("last_seen"))
        if seen is not None and seen.tzinfo is None:
            seen = seen.replace(tzinfo=now.tzinfo)
        if seen is None or seen < cutoff:
            continue
        kept[tid] = e
    if len(kept) > PROJECT_CAP:
        recent = sorted(kept, key=lambda t: kept[t]["last_seen"], reverse=True)[:PROJECT_CAP]
        kept = {t: kept[t] for t in recent}
    return kept


def record(cfg, project, ids, *, campaign, source, now=None) -> int:
    """Count one flake of each distinct id in `ids` for `project`; returns how many ids were
    recorded. Raises OSError / ValueError on a failed write (callers treat that as best-effort)."""
    if source not in SOURCES:
        raise ValueError(f"unknown flaky source {source!r}")
    ids = list(dict.fromkeys(str(i) for i in ids or () if i))
    if not ids:
        return 0
    now = now or dt.datetime.now().astimezone()
    ts = store.now_iso(now)
    d = store.agentic_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    with store._flocked(_lock_path(cfg)):
        doc = _load(cfg, now.strftime("%Y%m%dT%H%M%S"))
        entries = doc["projects"].setdefault(project, {})
        for tid in ids:
            e = entries.setdefault(tid, {"count": 0, "first_seen": ts, "last_seen": ts,
                                         "sources": {}, "campaigns": []})
            e["count"] = int(e.get("count") or 0) + 1
            e["last_seen"] = ts
            e.setdefault("sources", {})
            e["sources"][source] = int(e["sources"].get(source) or 0) + 1
            camps = [c for c in e.get("campaigns") or [] if c != campaign] + [campaign]
            e["campaigns"] = camps[-CAMPAIGNS_KEPT:]
        for name in list(doc["projects"]):
            doc["projects"][name] = _prune(doc["projects"][name], now)
            if not doc["projects"][name]:
                del doc["projects"][name]
        _atomic_write(path(cfg), json.dumps(doc, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    return len(ids)


def _read(cfg) -> dict:
    """Read-only view: never writes, never moves a bad file aside."""
    try:
        doc = json.loads(path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _fresh()
    return doc if _valid(doc) else _fresh()


def lookup(cfg, project, ids) -> dict:
    """`{id: entry}` for the ids of `project` that are on record."""
    entries = _read(cfg)["projects"].get(project) or {}
    return {i: entries[i] for i in ids if i in entries}


def list_entries(cfg, project=None) -> list:
    """Rows `{project, id, count, first_seen, last_seen, sources, campaigns}`, most-flaked first
    (then most recently seen), for one project or all."""
    rows = []
    for name, entries in _read(cfg)["projects"].items():
        if project and name != project:
            continue
        for tid, e in entries.items():
            rows.append({"project": name, "id": tid, **e})
    rows.sort(key=lambda r: (r.get("last_seen") or ""), reverse=True)
    rows.sort(key=lambda r: int(r.get("count") or 0), reverse=True)
    return rows
