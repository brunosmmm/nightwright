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
import re
import secrets
import shutil
import threading
from contextlib import contextmanager
from pathlib import Path

from ..export import _atomic_write
from ..rules import INTERNAL_PROJECT
from . import codewatch
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


def campaign_tmp(cfg, campaign_id) -> Path:
    """Parent of every child's private `TMPDIR` (SPEC-0253)."""
    return campaign_dir(cfg, campaign_id) / "tmp"


def child_tmp(cfg, campaign_id, owner: str, *, create: bool = False,
              exclusive: bool = False) -> Path:
    """`TMPDIR` of one gate run (`gate-<seq:03d>`) or persona run (`run-<run_id>`). Several
    children of a campaign are alive at once (reviewers, adopted runs, a human's `nightwright gate
    run`), so each gets its own directory and only its owner removes it. `exclusive` makes
    the creation an atomic claim: `FileExistsError` when the directory already exists."""
    path = campaign_tmp(cfg, campaign_id) / _tmp_owner(owner)
    if create or exclusive:
        if campaign_tmp(cfg, campaign_id).is_symlink():
            raise ValueError(f"refusing to create {path}: {campaign_tmp(cfg, campaign_id)} "
                             "is a symlink")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.mkdir(exist_ok=not exclusive)
    return path


def _tmp_owner(owner) -> str:
    if not isinstance(owner, str) or not owner or owner in (".", "..") \
            or "/" in owner or "\\" in owner or "\0" in owner:
        raise ValueError(f"invalid tmp owner {owner!r}")
    return owner


def clear_child_tmp(cfg, campaign_id, owner: str) -> bool:
    """Remove one child's tmp dir; refuses (ValueError) any target that is not strictly under
    `<campaign_dir>/tmp` — the campaign dir also holds `state.json` and `ledger.jsonl`."""
    root = campaign_dir(cfg, campaign_id).resolve() / "tmp"
    if root.is_symlink():                   # a link here would move the guard's root outside
        raise ValueError(f"refusing to remove under {root}: it is a symlink")
    target = root / _tmp_owner(owner)
    if target.is_symlink():
        target.unlink()
        return True
    resolved = target.resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError(f"refusing to remove {resolved}: not under {root}")
    if not resolved.exists():
        return False
    shutil.rmtree(resolved, ignore_errors=True)
    return True


def clear_campaign_tmp(cfg, campaign_id) -> bool:
    """Remove the whole `<campaign_dir>/tmp` tree (terminal states); refuses any other target."""
    cdir = campaign_dir(cfg, campaign_id).resolve()
    target = cdir / "tmp"
    if target.is_symlink():
        target.unlink()
        return True
    resolved = target.resolve()
    if resolved != cdir / "tmp" or not campaign_id:
        raise ValueError(f"refusing to remove {resolved}: not {cdir / 'tmp'}")
    if not resolved.exists():
        return False
    shutil.rmtree(resolved, ignore_errors=True)
    return True


def sweep_campaign_tmp(cfg, campaign_id) -> list[str]:
    """Residue of a killed runner: remove a child dir only when its owner is recorded finished
    (`gate-<seq>` with a `gate` event of that `gate_seq`, `run-<id>` with a `run.end`). An
    unfinished or unknown owner is left for the terminal-state removal. Returns the owners
    removed."""
    root = campaign_tmp(cfg, campaign_id)
    if root.is_symlink():
        return []
    try:
        names = [p.name for p in root.iterdir() if p.is_dir() and not p.is_symlink()]
    except OSError:
        return []
    if not names:
        return []
    gates, runs = set(), set()
    for ev in read_ledger(cfg, campaign_id, types=["gate", "run.end"]):
        if ev["type"] == "gate" and isinstance(ev.get("gate_seq"), int):
            gates.add(f"gate-{ev['gate_seq']:03d}")
        elif ev["type"] == "run.end" and ev.get("run_id"):
            runs.add(f"run-{ev['run_id']}")
    done = [n for n in sorted(names) if n in gates or n in runs]
    return [n for n in done if clear_child_tmp(cfg, campaign_id, n)]


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


# `new_campaign_id` shape: `<prefix>-<YYYYMMDD>-<4 hex>`. The prefix is whatever was minted —
# a SPEC/IDEA id, a red-main `events._safe` name (dots and Unicode alphanumerics stay inside
# the id), or an outbound `PROJ-NNNN` whose project code may start with a digit (`3M-0001`).
# The date-hex suffix is the anchor. A trailing `.` still ends it (`(?![\w-])`); a word
# character or hyphen glued to either side stays inside the token and does not match.
_CID_SUFFIX_RE = re.compile(r"-\d{8}-[0-9a-f]{4}(?![\w-])")


def _id_char(ch: str) -> bool:
    """A character `new_campaign_id` / `events._safe` can put inside an id."""
    return ch.isalnum() or ch in "._-"


def _campaign_id_spans(text: str) -> list[list[str]]:
    """Each date-hex hit, longest token first, then each suffix that starts after a `.`.

    A `.` inside a minted id (`FIX-acme.io-…`) is part of the token. A `.` before an id
    (`.CID`, `sentence.CID`) is punctuation: the whole span is not a campaign, and the suffix
    after the dot is. Callers keep the longest suffix that exists, so a dotted id stays one
    mention. A word character or hyphen glued on the left is not stripped — that span is one
    token and does not resolve."""
    spans = []
    for match in _CID_SUFFIX_RE.finditer(text):
        i = match.start()
        while i > 0 and _id_char(text[i - 1]):
            i -= 1
        raw = text[i:match.end()]
        if raw[:1] == ".":
            raw = raw.lstrip(".")
        if not raw or not raw[0].isalnum():
            continue
        alts = [raw]
        start = 0
        while True:
            dot = raw.find(".", start)
            if dot < 0:
                break
            suffix = raw[dot + 1:]
            if suffix[:1].isalnum():
                alts.append(suffix)
            start = dot + 1
        spans.append(alts)
    return spans


def _contained_campaign_dir(cfg, campaign_id) -> Path | None:
    """The campaign directory when `campaign_id` is a single name under the campaigns root.

    Absolute paths, separators, `.` / `..`, and a symlink that resolves outside the root are
    not campaigns. Inherited event and message stamps pass through here so they cannot read
    or write outside the store (SPEC-0431)."""
    if not isinstance(campaign_id, str) or not campaign_id or campaign_id in (".", ".."):
        return None
    if "/" in campaign_id or "\\" in campaign_id or "\0" in campaign_id:
        return None
    if Path(campaign_id).is_absolute():
        return None
    root = campaigns_dir(cfg).resolve()
    try:
        target = (root / campaign_id).resolve()
    except (OSError, RuntimeError):
        return None
    if target == root or root not in target.parents:
        return None
    return target


def campaign_state(cfg, campaign_id) -> dict | None:
    """The campaign's state object, or None when it is missing, unreadable, outside the
    campaigns root, or not a JSON object. Inference accepts only this (SPEC-0431): a list,
    string, or other JSON value is not a campaign, and a non-string id is not an error."""
    if _contained_campaign_dir(cfg, campaign_id) is None:
        return None
    try:
        st = read_state(cfg, campaign_id)
    except (ValueError, OSError, TypeError):
        return None
    return st if isinstance(st, dict) else None


def sole_campaign_named(cfg, text) -> str | None:
    """The campaign id `text` names, iff it names exactly one distinct id that resolves to a
    readable campaign (SPEC-0431). Matches every id `new_campaign_id` mints, including red-main
    ids and outbound ids whose project code starts with a digit. A period before an id is
    punctuation (`.CID`, `sentence.CID`); a period inside a dotted id stays part of that id.
    Unknown, unreadable, or non-object candidates are dropped before counting; zero or
    two-or-more survivors give None. Pure read; never raises."""
    try:
        found = []
        seen = set()
        for alts in _campaign_id_spans(text or ""):
            chosen = next((cid for cid in alts if campaign_state(cfg, cid) is not None), None)
            if chosen is not None and chosen not in seen:
                seen.add(chosen)
                found.append(chosen)
        return found[0] if len(found) == 1 else None
    except Exception:
        return None


def create_campaign(cfg, state: dict, *, by="human") -> Path:
    """Create the directory (fails if it exists), write `state.json`, append the `open` event."""
    cid = state["id"]
    d = campaign_dir(cfg, cid)
    d.parent.mkdir(parents=True, exist_ok=True)
    d.mkdir()  # FileExistsError on a duplicate id — never silently reuse a campaign
    state = dict(state)
    state.setdefault("schema", "nw.campaign.v1")
    state.setdefault("created_at", now_iso())
    lean_off = []
    if "lean" not in state:                # SPEC-0553: pin the lean flags once, at open
        from . import tiers as T
        req = state.get("lean_request")    # SPEC-0558: the campaign's own opt-in wins
        flags, lean_off = T.effective_lean(cfg, state.get("project"), request=req)
        if any(flags.values()) or lean_off or req is not None:
            state["lean"] = flags
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
        **({"profile_sources": state["profile_sources"]}              # SPEC-0238
           if state.get("profile_sources") else {}),
        "admission": state.get("admission"),                              # SPEC-0243
        "nw_sha": codewatch.code_sha(),                                   # SPEC-0397
        "profile_sha256": state.get("profile_sha256"),
        **({"kind": "merge", "merge_branch": (state.get("merge") or {}).get("branch")}
           if state.get("kind") == "merge" else {}),               # SPEC-0233
        **({"docs_only": True} if (state.get("merge") or {}).get("docs_only") else {}),  # SPEC-0234
        **({"lean": state["lean"]} if "lean" in state else {}),        # SPEC-0553
        **({"lean_off": lean_off} if lean_off else {}),
        **({"lean_request": state["lean_request"]}                    # SPEC-0558
           if state.get("lean_request") is not None else {}),
        **({"cohort": state["cohort"]} if state.get("cohort") else {}),
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
    duplicate re-check and `create_campaign` of `nw queue add`, so two concurrent adds of the
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


EVENT_UID_SCAN = 200          # SPEC-0341: how far back `append_event` looks for an `event_uid`


def _stamp_finding(cfg, campaign_id, rec: dict) -> None:
    """SPEC-0408: a `finding` always carries `run_id` (None when no persona run produced it)
    and `key` (`followups.finding_key`, the follow-up key). An explicit value is kept; when
    the campaign state cannot be read the key is left out rather than failing the append."""
    rec.setdefault("run_id", None)
    if "key" in rec:
        return
    from . import followups                  # imports queue, which imports this module
    try:
        spec_id = read_state(cfg, campaign_id).get("spec_id")
    except (ValueError, OSError):            # ValueError includes JSONDecodeError
        return
    try:
        rec["key"] = followups.finding_key(spec_id, rec.get("title"), rec.get("files"))
    except (TypeError, ValueError):          # e.g. `files` is not a list: leave the key off
        return


def append_event(cfg, campaign_id, type, payload=None, *, by="tick") -> dict:
    """Append one `nw.ledger.event.v1` line `{schema, ts, seq, campaign, type, by, **payload}`
    under the campaign lock, with `O_APPEND` and a single `write()` so lines never interleave.
    The record is validated first (SPEC-0192): an unknown type or a missing per-type key
    raises ValueError and nothing is written. SPEC-0341: a payload carrying `event_uid` is
    skipped (the existing record is returned) when that uid is already among the ledger's
    last `EVENT_UID_SCAN` entries; a payload without one is appended with no scan."""
    path = _ledger_path(cfg, campaign_id)
    with item_lock(cfg, campaign_id):
        uid = (payload or {}).get("event_uid")
        if uid is not None:
            for old in reversed(read_ledger(cfg, campaign_id, tail=EVENT_UID_SCAN)):
                if old.get("event_uid") == uid:
                    return old
        rec = {"schema": SC.LEDGER_SCHEMA, "ts": now_iso(), "seq": _last_seq(path) + 1,
               "campaign": campaign_id, "type": type, "by": by}
        rec.update(payload or {})
        if type == "finding":
            _stamp_finding(cfg, campaign_id, rec)
        problems = SC.ledger_problems(rec)
        if problems:
            raise ValueError(f"invalid ledger event for {campaign_id}: {'; '.join(problems)}")
        line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    if type == "run.end" and rec.get("run_id"):     # SPEC-0253: the run's result is recorded
        try:
            clear_child_tmp(cfg, campaign_id, f"run-{rec['run_id']}")
        except ValueError:
            pass
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

_STATE_CACHE: dict = {}      # SPEC-0372: path -> ((mtime_ns, size), raw JSON text parse)


def _load_state_cached(p):
    """Parse `state.json` only when its (mtime_ns, size) changed; each caller gets a private
    deep copy. `nw queue watch` lists every campaign several times per refresh."""
    import pickle
    try:
        stt = p.stat()
    except OSError:
        return None
    sig = (stt.st_mtime_ns, stt.st_size)
    hit = _STATE_CACHE.get(p)
    if hit and hit[0] == sig:
        return pickle.loads(hit[1])
    st = json.loads(p.read_text(encoding="utf-8"))
    _STATE_CACHE[p] = (sig, pickle.dumps(st, pickle.HIGHEST_PROTOCOL))
    return st


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
            st = _load_state_cached(p)
        except ValueError:
            continue
        if st is None:
            continue
        if not include_terminal and st.get("state") in TERMINAL_STATES:
            continue
        if project and (st.get("project") or INTERNAL_PROJECT) != project:   # old states: wt's own
            continue
        if spec_id and st.get("spec_id") != spec_id:
            continue
        if state and st.get("state") != state:
            continue
        out.append(st)
    out.sort(key=lambda s: (s.get("created_at") or "", s.get("id") or ""), reverse=True)
    return out


def active_campaign_for_spec(cfg, spec_id, project=None) -> dict | None:
    """The one non-terminal campaign for `spec_id`, or None. Spec ids are per project
    (SPEC-0327): with `project`, only that project's campaigns count."""
    hits = list_campaigns(cfg, spec_id=spec_id, project=project)
    return hits[0] if hits else None
