"""Campaign store (SPEC-0171): one directory per campaign under `<data_dir>/agentic/campaigns/`
holding an atomically written `state.json` and an append-only `ledger.jsonl`.

Writers take the campaign's `flock` (`.lock`) around every read-modify-write and every append,
so the loop and human commands can touch the same campaign at once. The queue is a scan of
`state.json` files — there is no second index to drift.

`item_lock` is reentrant within a process (SPEC-0176): a per-campaign `threading.RLock` plus a
depth counter take the `flock` only at depth 1, so the queue can append events while it holds
the lock for a read-modify-write. Across processes (or two fds in one process that bypass this
helper) the `flock` still excludes."""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import secrets
import threading
from contextlib import contextmanager
from pathlib import Path

from ..export import _atomic_write
from . import schemas as SC

TERMINAL_STATES = frozenset({"MERGED", "HANDED_OFF", "ABANDONED"})


# ---- paths ------------------------------------------------------------------------------

def agentic_dir(cfg) -> Path:
    return Path(cfg["data_dir"]) / "agentic"


def campaigns_dir(cfg) -> Path:
    return agentic_dir(cfg) / "campaigns"


def campaign_dir(cfg, campaign_id) -> Path:
    return campaigns_dir(cfg) / campaign_id


def runs_dir(cfg, campaign_id) -> Path:
    return campaign_dir(cfg, campaign_id) / "runs"


def gates_dir(cfg, campaign_id) -> Path:
    return campaign_dir(cfg, campaign_id) / "gates"


def _state_path(cfg, campaign_id) -> Path:
    return campaign_dir(cfg, campaign_id) / "state.json"


def _ledger_path(cfg, campaign_id) -> Path:
    return campaign_dir(cfg, campaign_id) / "ledger.jsonl"


def now_iso(now=None) -> str:
    return (now or dt.datetime.now().astimezone()).isoformat(timespec="seconds")


# ---- ids and lifecycle ---------------------------------------------------------------------

def new_campaign_id(cfg, spec_id, now=None) -> str:
    """`<SPEC-ID>-<YYYYMMDD>-<4 hex>`; re-rolled while the directory exists."""
    day = (now or dt.datetime.now()).strftime("%Y%m%d")
    for _ in range(32):
        cid = f"{spec_id}-{day}-{secrets.token_hex(2)}"
        if not campaign_dir(cfg, cid).exists():
            return cid
    raise ValueError(f"could not mint a free campaign id for {spec_id}")


def create_campaign(cfg, state: dict, *, by="human") -> Path:
    """Create the directory (fails if it exists), write `state.json`, append the `open` event."""
    cid = state["id"]
    d = campaign_dir(cfg, cid)
    d.parent.mkdir(parents=True, exist_ok=True)
    d.mkdir()  # FileExistsError on a duplicate id — never silently reuse a campaign
    state = dict(state)
    state.setdefault("schema", "wt.campaign.v1")
    state.setdefault("created_at", now_iso())
    write_state(cfg, cid, state)
    repo = state.get("repo") or {}
    sizing = state.get("sizing") or {}
    append_event(cfg, cid, "open", {
        "spec_id": state.get("spec_id"), "project": state.get("project"),
        "profile": state.get("profile"), "base": repo.get("base"),
        "base_sha": repo.get("base_sha_at_open"), "worktree": repo.get("worktree"),
        "branch": repo.get("branch"),
        **({"size": sizing.get("size"), "tone": sizing.get("tone"),       # SPEC-0219
            "profile_source": sizing.get("source"), "profile_reason": sizing.get("reason")}
           if sizing else {}),
    }, by=by)
    return d


def read_state(cfg, campaign_id) -> dict:
    path = _state_path(cfg, campaign_id)
    if not path.is_file():
        raise ValueError(f"no campaign {campaign_id!r} under {campaigns_dir(cfg)}")
    return json.loads(path.read_text(encoding="utf-8"))


def write_state(cfg, campaign_id, state: dict) -> None:
    """Atomic replace (mkstemp + os.replace via export._atomic_write); stamps `updated_at`."""
    state = dict(state)
    state["updated_at"] = now_iso()
    _atomic_write(_state_path(cfg, campaign_id),
                  json.dumps(state, indent=2, ensure_ascii=False) + "\n")


_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, tuple[threading.RLock, dict]] = {}   # lock file path -> (rlock, {"depth", "fd"})


def _process_lock(path: Path):
    with _LOCKS_GUARD:
        entry = _LOCKS.get(str(path))
        if entry is None:
            entry = (threading.RLock(), {"depth": 0, "fd": None})
            _LOCKS[str(path)] = entry
        return entry


@contextmanager
def _flocked(lock_file: Path):
    """Process RLock + exclusive `flock` on `lock_file`, reentrant within the process."""
    rlock, meta = _process_lock(lock_file)
    with rlock:
        if meta["depth"] == 0:
            fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except BaseException:
                os.close(fd)
                raise
            meta["fd"] = fd
        meta["depth"] += 1
        try:
            yield
        finally:
            meta["depth"] -= 1
            if meta["depth"] == 0:
                fd, meta["fd"] = meta["fd"], None
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)


@contextmanager
def item_lock(cfg, campaign_id):
    """Exclusive `flock` on `campaigns/<id>/.lock` for the duration of the block. Reentrant
    within the process: nested use by the same thread re-enters the RLock and leaves the
    single `flock` in place; other threads wait on the RLock; other processes wait on the
    `flock`."""
    d = campaign_dir(cfg, campaign_id)
    d.mkdir(parents=True, exist_ok=True)
    with _flocked(d / ".lock"):
        yield


@contextmanager
def add_lock(cfg):
    """Queue-wide exclusive lock on `<agentic>/add.lock` (SPEC-0209): held around the
    duplicate re-check and `create_campaign` of `wt queue add`, so two concurrent adds of the
    same spec or idea create exactly one campaign. Built like `item_lock`."""
    d = agentic_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    with _flocked(d / "add.lock"):
        yield


# ---- ledger ----------------------------------------------------------------------------

def _last_seq(path: Path) -> int:
    """`seq` of the last parseable line (0 when empty/missing). Re-read under the lock on
    every append, so a truncated or foreign last line can never make seq go backwards."""
    if not path.is_file():
        return 0
    last = 0
    with open(path, "rb") as f:
        for raw in f:
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if isinstance(rec, dict) and isinstance(rec.get("seq"), int):
                last = max(last, rec["seq"])
    return last


def append_event(cfg, campaign_id, type, payload=None, *, by="tick") -> dict:
    """Append one `wt.ledger.event.v1` line `{schema, ts, seq, campaign, type, by, **payload}`
    under the campaign lock, with `O_APPEND` and a single `write()` so lines never interleave.
    The record is validated first (SPEC-0192): an unknown type or a missing per-type key
    raises ValueError and nothing is written."""
    path = _ledger_path(cfg, campaign_id)
    with item_lock(cfg, campaign_id):
        rec = {"schema": SC.LEDGER_SCHEMA, "ts": now_iso(), "seq": _last_seq(path) + 1,
               "campaign": campaign_id, "type": type, "by": by}
        rec.update(payload or {})
        problems = SC.ledger_problems(rec)
        if problems:
            raise ValueError(f"invalid ledger event for {campaign_id}: {'; '.join(problems)}")
        line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    return rec


def read_ledger(cfg, campaign_id, *, types=None, tail=None) -> list[dict]:
    """Events in file order; unparseable lines (e.g. a truncated last line) are skipped."""
    path = _ledger_path(cfg, campaign_id)
    if not path.is_file():
        return []
    out = []
    wanted = set(types) if types else None
    with open(path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if wanted and rec.get("type") not in wanted:
                continue
            out.append(rec)
    if tail:
        out = out[-tail:]
    return out


# ---- listing ----------------------------------------------------------------------------

def list_campaigns(cfg, *, include_terminal=False, project=None, spec_id=None,
                   state=None) -> list[dict]:
    """Scan `campaigns/*/state.json`, newest first by `created_at`. Hides terminal states
    unless `include_terminal`; optional exact filters on project / spec_id / state."""
    root = campaigns_dir(cfg)
    if not root.is_dir():
        return []
    out = []
    for d in root.iterdir():
        p = d / "state.json"
        if not p.is_file():
            continue
        try:
            st = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if not include_terminal and st.get("state") in TERMINAL_STATES:
            continue
        if project and st.get("project") != project:
            continue
        if spec_id and st.get("spec_id") != spec_id:
            continue
        if state and st.get("state") != state:
            continue
        out.append(st)
    out.sort(key=lambda s: (s.get("created_at") or "", s.get("id") or ""), reverse=True)
    return out


def active_campaign_for_spec(cfg, spec_id) -> dict | None:
    """The one non-terminal campaign for `spec_id`, or None."""
    hits = list_campaigns(cfg, spec_id=spec_id)
    return hits[0] if hits else None
