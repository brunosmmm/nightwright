"""Disposable campaign history cache (SPEC-0472)."""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import secrets
import shutil
import threading
from contextlib import contextmanager
from pathlib import Path

from . import store

_NOT_WRITABLE = "cache: not writable"
_TERMINAL_OUTCOMES = frozenset({"merged", "handed_off", "abandoned"})
_LOCAL_SOURCES = frozenset({"ledger", "run"})
_COMPUTE = object()


def _lazy_history():
    from . import history as H
    return H


def _lazy_conductor():
    from . import conductor_cost as CC
    return CC


def _lazy_schemas():
    from . import schemas as SC
    return SC


def _v1_root(cfg) -> Path:
    cache = Path(cfg["cache_dir"]).resolve()
    v1 = (cache / "history" / "v1").resolve()
    try:
        v1.relative_to(cache)
    except ValueError:
        raise OSError("cache: not writable")
    return v1


def _data_root(cfg) -> Path:
    return Path(cfg["data_dir"]).resolve()


def _not_writable(cfg, cache) -> bool:
    if cache is False:
        return False
    cd = cfg.get("cache_dir")
    if cache is True and (not cd or not str(cd).strip()):
        return True
    if not cd:
        return False
    path = Path(cd)
    if path.is_file():
        return True
    data = _data_root(cfg)
    try:
        v1 = (path / "history" / "v1").resolve()
    except OSError:
        return True
    if v1 == data or data in v1.parents:
        return True
    if not path.exists():
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            return True
    elif not path.is_dir():
        return True
    try:
        v1.mkdir(parents=True, exist_ok=True)
    except OSError:
        return True
    if not v1.is_dir():
        return True
    return False


def _writable_root(cfg, cache):
    if _not_writable(cfg, cache):
        return None
    return _v1_root(cfg)


def _tmp_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    with tmp.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


def _atomic_write_json(path: Path, value) -> None:
    _atomic_write_text(path, json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n")


def _is_tmp(name: str) -> bool:
    return ".tmp-" in name


def _file_entry(path: Path) -> dict | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "inode": st.st_ino, "offset": st.st_size}


def _log_entry(path: Path, offset: int | None = None, *, prev: dict | None = None) -> dict | None:
    row = _file_entry(path)
    if row is None:
        return None
    if offset is not None:
        row["offset"] = offset
    elif prev is not None:
        same = (prev.get("inode"), prev.get("size")) == (row.get("inode"), row.get("size"))
        if same:
            row["offset"] = prev.get("offset", row["size"])
        elif prev.get("inode") == row.get("inode") and row.get("size", 0) > prev.get("size", 0):
            row["offset"] = _complete_log_offset_from(path, int(prev.get("offset", 0)))
        else:
            row["offset"] = _complete_log_offset(path)
    else:
        row["offset"] = _complete_log_offset(path)
    return row


def _complete_log_offset_from(path: Path, start: int) -> int:
    if not path.is_file():
        return 0
    try:
        with path.open("rb") as stream:
            stream.seek(start)
            offset = start
            while True:
                pos = stream.tell()
                raw = stream.readline()
                if not raw:
                    return offset
                if raw.endswith(b"\n"):
                    offset = stream.tell()
                else:
                    return pos
    except OSError:
        return start


def _complete_log_offset(path: Path) -> int:
    """Byte position after the last complete newline (trailing partial line excluded)."""
    if not path.is_file():
        return 0
    try:
        with path.open("rb") as stream:
            offset = 0
            while True:
                pos = stream.tell()
                raw = stream.readline()
                if not raw:
                    return offset
                if raw.endswith(b"\n"):
                    offset = stream.tell()
                else:
                    return pos
    except OSError:
        return 0


def _tree_sig(root: Path) -> str:
    if not root.is_dir():
        return hashlib.sha1(b"").hexdigest()
    lines = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or _is_tmp(path.name):
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        rel = path.relative_to(root).as_posix()
        lines.append(f"{rel} {st.st_size} {st.st_ino} {st.st_mtime_ns}")
    return hashlib.sha1("\n".join(lines).encode("utf-8")).hexdigest()


def _search_text(rec: dict) -> str:
    return f"{rec.get('title') or ''} {rec.get('text') or ''}".casefold()


def _stored_line(rec: dict) -> str:
    return json.dumps({**rec, "search": _search_text(rec)}, ensure_ascii=False)


_records_mem: dict[tuple, list[tuple[dict, str]]] = {}
_CACHE_WRITE_LOCK = threading.Lock()


@contextmanager
def _cache_write_guard(root: Path):
    with _CACHE_WRITE_LOCK:
        root.mkdir(parents=True, exist_ok=True)
        lock_path = root / ".lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
            if not lock_path.is_file():
                with lock_path.open("ab"):
                    pass
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _records_cache_key(root: Path) -> tuple | None:
    path = root / "records.jsonl"
    try:
        st = path.stat()
    except OSError:
        return None
    return (str(root.resolve()), st.st_ino, st.st_size, st.st_mtime_ns)


def _drop_stale_records_cache(root: Path) -> None:
    prefix = str(root.resolve())
    current = _records_cache_key(root)
    for key in list(_records_mem):
        if key[0] == prefix and key != current:
            del _records_mem[key]


def _set_records_mem(root: Path, entries: list[tuple[dict, str]]) -> None:
    _drop_stale_records_cache(root)
    key = _records_cache_key(root)
    if key is not None:
        _records_mem[key] = entries


def _get_records_mem(root: Path) -> list[tuple[dict, str]] | None:
    key = _records_cache_key(root)
    if key is None:
        return None
    return _records_mem.get(key)


def _load_records_mem(root: Path) -> list[tuple[dict, str]]:
    path = root / "records.jsonl"
    if not path.is_file():
        return []
    out: list[tuple[dict, str]] = []
    try:
        with path.open(encoding="utf-8") as stream:
            for raw in stream:
                if not raw.strip():
                    continue
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                if isinstance(row.get("record"), dict):
                    rec = dict(row["record"])
                    search = row.get("search")
                else:
                    rec = {k: v for k, v in row.items() if k != "search"}
                    search = row.get("search")
                if not isinstance(search, str):
                    search = _search_text(rec)
                out.append((rec, search))
    except OSError:
        return []
    _set_records_mem(root, out)
    return out


def _read_json(path: Path):
    try:
        if not path.is_file() or _is_tmp(path.name):
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None


def _read_index(root: Path) -> list[dict]:
    path = root / "index.jsonl"
    if not path.is_file():
        return []
    rows = []
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    except OSError:
        return []
    return rows


def _read_records(root: Path) -> list[dict]:
    cached = _get_records_mem(root)
    if cached is not None:
        return [rec for rec, _ in cached]
    return [rec for rec, _ in _load_records_mem(root)]


def _record_line_matches(raw: str, *, kinds: set[str] | None, needle: str) -> bool:
    """Reject lines that cannot match before json.loads (kind and coarse query prefilter)."""
    if kinds is not None:
        if not any(f'"kind": "{kind}"' in raw for kind in kinds):
            return False
    return True


def _read_records_filtered(
    root: Path,
    *,
    kinds: set[str] | None = None,
    needle: str = "",
) -> list[dict]:
    """Load records from JSONL, skipping lines that cannot match kind/query filters."""
    cached = _get_records_mem(root)
    if cached is not None:
        out = []
        for rec, search in cached:
            if kinds is not None and rec.get("kind") not in kinds:
                continue
            if needle and needle not in search:
                continue
            out.append(rec)
        return out
    path = root / "records.jsonl"
    if not path.is_file():
        return []
    out = []
    prefilter = kinds is not None or bool(needle)
    try:
        with path.open(encoding="utf-8") as stream:
            for raw in stream:
                line = raw.strip()
                if not line:
                    continue
                if prefilter and not _record_line_matches(raw, kinds=kinds, needle=needle):
                    continue
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                if isinstance(row.get("record"), dict):
                    rec = dict(row["record"])
                else:
                    rec = {k: v for k, v in row.items() if k != "search"}
                if kinds is not None and rec.get("kind") not in kinds:
                    continue
                search = row.get("search")
                if not isinstance(search, str):
                    search = _search_text(rec)
                if needle and needle not in search:
                    continue
                out.append(rec)
    except OSError:
        return []
    return out


def _write_index(root: Path, rows: list[dict]) -> None:
    text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    _atomic_write_text(root / "index.jsonl", text)


def _write_records(root: Path, records: list[dict]) -> None:
    text = "".join(_stored_line(rec) + "\n" for rec in records)
    _atomic_write_text(root / "records.jsonl", text)
    _set_records_mem(root, [(rec, _search_text(rec)) for rec in records])


def _sources_ok(sources) -> bool:
    return isinstance(sources, dict) and sources.get("version") == 1 and sources.get("complete") is True


def _scan_sources(
    cfg,
    prev: dict | None = None,
    *,
    assume_full_read: frozenset[str] | None = None,
    fixed_offsets: dict[str, int] | None = None,
) -> dict:
    H = _lazy_history()
    agentic = store.agentic_dir(cfg)
    prev_logs = (prev or {}).get("logs") or {}
    logs = {}
    for key in ("conductor/decisions.jsonl", "conductor/sessions.jsonl",
                "spec-reviews.jsonl", "runner.jsonl"):
        path = agentic / key
        if not path.is_file():
            continue
        prev_log = prev_logs.get(key)
        if assume_full_read and key in assume_full_read:
            entry = _file_entry(path)
            if entry is not None:
                entry["offset"] = entry["size"]
                logs[key] = entry
            continue
        if fixed_offsets is not None and key in fixed_offsets:
            entry = _file_entry(path)
            if entry is not None:
                entry["offset"] = min(fixed_offsets[key], int(entry["size"]))
                logs[key] = entry
            continue
        entry = _log_entry(path, prev=prev_log)
        if entry is not None:
            logs[key] = entry
    mailbox = agentic / "mailbox"
    if mailbox.is_dir():
        for path in sorted(mailbox.glob("*.jsonl")):
            key = f"mailbox/{path.name}"
            if fixed_offsets is not None and key in fixed_offsets:
                entry = _file_entry(path)
                if entry is not None:
                    entry["offset"] = min(fixed_offsets[key], int(entry["size"]))
                    logs[key] = entry
                continue
            entry = _file_entry(path)
            if entry is not None:
                entry["offset"] = entry["size"]
                logs[key] = entry
    files = {}
    flaky = agentic / "flaky-tests.json"
    entry = _file_entry(flaky)
    if entry is not None:
        files["flaky-tests.json"] = entry
    trees = {
        "escalations": _tree_sig(agentic / "escalations"),
        "reviews": _tree_sig(agentic / "reviews"),
        "sentinel": _tree_sig(agentic / "sentinel"),
    }
    CC = _lazy_conductor()
    prev_transcripts = (prev or {}).get("transcripts") or {}
    transcripts = {}
    for row in CC.sessions(cfg):
        path = CC.find_transcript(cfg, row["session_id"])
        if path is not None:
            resolved = str(path.resolve())
            entry = _file_entry(path)
            if entry is not None:
                transcripts[resolved] = entry
    return {
        "version": 1,
        "complete": False,
        "logs": logs,
        "files": files,
        "trees": trees,
        "transcripts": transcripts,
    }


def _iso_window(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.isoformat(timespec="seconds")
    return str(value)


def _window_rows(index_rows):
    out = []
    for row in index_rows:
        start = _lazy_history()._parse_ts(row.get("start"))
        live_end = _lazy_history()._parse_ts(row.get("live_end"))
        out.append({"campaign": row["campaign"], "start": start, "live_end": live_end})
    return out


def _serialize_turns(turns: list[dict]) -> list[dict]:
    out = []
    for turn in turns:
        row = dict(turn)
        ts = row.get("ts")
        if isinstance(ts, dt.datetime):
            row["ts"] = ts.isoformat(timespec="seconds")
        out.append(row)
    return out


def _apply_turn_cursors(sources: dict, turn_cursor: dict) -> None:
    if turn_cursor.get("sessions"):
        sources.setdefault("logs", {})["conductor/sessions.jsonl"] = turn_cursor["sessions"]
    if turn_cursor.get("transcripts"):
        sources["transcripts"] = dict(turn_cursor["transcripts"])


def _log_cursor_entries(cfg, parsed_offsets: dict[str, int]) -> dict[str, dict]:
    """Log cursors for a cold fill: never publish size past bytes parsed at fill time."""
    agentic = store.agentic_dir(cfg)
    logs: dict[str, dict] = {}
    for key, off in parsed_offsets.items():
        path = agentic / key
        if not path.is_file():
            continue
        entry = _file_entry(path)
        if entry is None:
            continue
        entry["offset"] = off
        if int(entry["size"]) > off:
            entry["size"] = off
        logs[key] = entry
    return logs


def _deserialize_turns(turns: list) -> list[dict]:
    out = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        row = dict(turn)
        ts = row.get("ts")
        if isinstance(ts, str):
            try:
                row["ts"] = dt.datetime.fromisoformat(ts)
            except ValueError:
                pass
        out.append(row)
    return out


def _fresh_conductor_turns(cfg, *, cursor_out: dict | None = None) -> list[dict]:
    """Load priced turns; optionally publish session/transcript cursors at each read."""
    CC = _lazy_conductor()
    agentic = store.agentic_dir(cfg)
    sessions_path = agentic / "conductor/sessions.jsonl"
    sess_stat = None
    sess_published = 0
    if sessions_path.is_file():
        try:
            sess_stat = sessions_path.stat()
            sess_published = sess_stat.st_size
        except OSError:
            sess_stat = None
    session_rows = CC.sessions(cfg)
    sessions_parsed = _complete_log_offset(sessions_path) if sessions_path.is_file() else 0
    if cursor_out is not None and sess_stat is not None:
        cursor_out["sessions"] = {
            "size": sess_published,
            "mtime_ns": sess_stat.st_mtime_ns,
            "inode": sess_stat.st_ino,
            "offset": sessions_parsed,
        }
    paths = [p for r in session_rows if (p := CC.find_transcript(cfg, r["session_id"]))]
    seen: set = set()
    out: list[dict] = []
    transcripts: dict = {}
    for path in paths:
        t_stat = None
        if path.is_file():
            try:
                t_stat = path.stat()
            except OSError:
                t_stat = None
        pre_size = int(t_stat.st_size) if t_stat is not None else None
        for turn in CC.read_turns(path):
            msg_id = turn.get("msg_id")
            if msg_id is not None:
                if msg_id in seen:
                    continue
                seen.add(msg_id)
            price = CC.price_for(cfg, turn["model"])
            out.append({
                **turn,
                "usd": CC.turn_usd(turn, price),
                "_price": price,
            })
        if cursor_out is not None and t_stat is not None and pre_size is not None:
            published = pre_size
            try:
                post = path.stat()
            except OSError:
                post = None
            if post is not None and post.st_size > published:
                published = pre_size
            transcripts[str(path.resolve())] = {
                "size": published,
                "mtime_ns": t_stat.st_mtime_ns,
                "inode": t_stat.st_ino,
                "offset": published,
            }
    if cursor_out is not None:
        cursor_out["transcripts"] = transcripts
    return sorted(out, key=lambda t: t["ts"])


def _load_turns(
    cfg,
    root: Path,
    sources: dict | None,
    *,
    snap: dict | None = None,
    sources_out: dict | None = None,
):
    turns_path = root / "conductor_turns.json"
    cached = _read_json(turns_path)
    snap = snap or _scan_sources(cfg, prev=sources)
    if sources and cached and snap.get("logs", {}).get("conductor/sessions.jsonl") == (
            sources.get("logs") or {}).get("conductor/sessions.jsonl") \
            and not _transcripts_need_refresh(cfg, sources, snap):
        if isinstance(cached, dict) and isinstance(cached.get("turns"), list):
            return _deserialize_turns(cached["turns"])
    turn_cursor: dict = {}
    turns = _fresh_conductor_turns(cfg, cursor_out=turn_cursor)
    if sources_out is not None:
        _apply_turn_cursors(sources_out, turn_cursor)
    return turns


def _conductor_shares(cfg, root: Path, index_rows, sources: dict | None) -> dict | None:
    CC = _lazy_conductor()
    turns = _load_turns(cfg, root, sources)
    if not any(isinstance(t.get("usd"), (int, float)) for t in turns):
        return None
    windows = _window_rows(index_rows)
    shares, _ = CC._split(turns, windows)
    return shares


def _apply_conductor(rows: list[dict], shares: dict | None) -> None:
    for row in rows:
        cid = row.get("campaign")
        if shares is None:
            row["conductor_usd"] = None
        else:
            row["conductor_usd"] = shares.get(cid, 0.0)


def _public_index_row(row: dict) -> dict:
    out = dict(row)
    out.pop("start", None)
    out.pop("live_end", None)
    return out


def _sort_index(rows):
    H = _lazy_history()
    return sorted(rows, key=lambda row: (
        H._time_key(row.get("closed_at") or row.get("created_at")), row.get("campaign") or ""
    ), reverse=True)


_CAMPAIGN_LISTING: dict[str, tuple[int, frozenset[str]]] = {}


def _live_campaign_ids(cfg) -> frozenset[str]:
    root = store.campaigns_dir(cfg)
    if not root.is_dir():
        return frozenset()
    try:
        st = root.stat()
    except OSError:
        return frozenset()
    key = str(root.resolve())
    prev = _CAMPAIGN_LISTING.get(key)
    if prev and prev[0] == st.st_mtime_ns:
        return prev[1]
    try:
        names = frozenset(
            e.name for e in os.scandir(root) if e.is_dir(follow_symlinks=False)
        )
    except OSError:
        names = frozenset()
    _CAMPAIGN_LISTING[key] = (st.st_mtime_ns, names)
    return names


def _campaign_ids(cfg) -> list[str]:
    return sorted(_live_campaign_ids(cfg))


def _live_window_for_campaign(cfg, cid: str) -> dict:
    """One campaign's conductor window; same rules as ``conductor_cost.live_windows``."""
    H = _lazy_history()
    try:
        state = store.read_state(cfg, cid)
    except ValueError:
        return {}
    events = store.read_ledger(cfg, cid, types=["open", "transition"])
    start = next((H._parse_ts(e.get("ts")) for e in events if e.get("type") == "open"), None) \
        or H._parse_ts(state.get("created_at"))
    term = next((e for e in reversed(events) if e.get("type") == "transition"
                 and e.get("to") in store.TERMINAL_STATES), None)
    end = H._parse_ts(term.get("ts")) if term else None
    live_end = end
    if end is None and state.get("state") in store.TERMINAL_STATES:
        last = next((H._parse_ts(e.get("ts")) for e in reversed(events)
                     if e.get("type") == "transition"), None)
        bounds = [t for t in (H._parse_ts(state.get("updated_at")), last) if t is not None]
        live_end = max(start, min(bounds)) if bounds and start else start
    return {"campaign": cid, "start": start, "live_end": live_end}


def _live_refresh_targets(cfg, index_rows: list[dict]) -> tuple[list[str], list[str]]:
    live = [r["campaign"] for r in index_rows if r.get("outcome") is None]
    missing = sorted(set(_campaign_ids(cfg)) - {r.get("campaign") for r in index_rows})
    return live, missing


def _local_records(doc: dict) -> list[dict]:
    return [rec for rec in doc.get("records") or [] if rec.get("source") in _LOCAL_SOURCES]


def _merge_records(existing: list[dict], *, drop_campaign: str | None = None,
                  drop_campaign_all: str | None = None,
                  drop_predicate=None, add: list[dict] | None = None) -> list[dict]:
    out = []
    for rec in existing:
        if drop_campaign_all and rec.get("campaign") == drop_campaign_all:
            continue
        if drop_campaign and rec.get("campaign") == drop_campaign and rec.get("source") in _LOCAL_SOURCES:
            continue
        if drop_predicate and drop_predicate(rec):
            continue
        out.append(rec)
    if add:
        out.extend(add)
    return out


def _records_for_campaign(doc: dict, cid: str) -> list[dict]:
    return [rec for rec in doc.get("records") or [] if rec.get("campaign") == cid]


def _merge_record_writes(start: list[dict], target: list[dict], disk: list[dict]) -> list[dict]:
    """Merge concurrent global writes into a records snapshot about to be replaced."""
    start_by = {r["id"]: r for r in start if r.get("id")}
    disk_by = {r["id"]: r for r in disk if r.get("id")}
    out: list[dict] = []
    seen: set[str] = set()
    for row in target:
        rid = row.get("id")
        if rid and rid in disk_by:
            disk_row = disk_by[rid]
            prev = start_by.get(rid)
            if prev is not None and disk_row != prev:
                row = disk_row
        if rid:
            seen.add(rid)
        out.append(row)
    target_ids = {r.get("id") for r in target if r.get("id")}
    for rid, disk_row in disk_by.items():
        if rid in target_ids or rid in seen:
            continue
        prev = start_by.get(rid)
        if prev is None or disk_row != prev:
            out.append(disk_row)
    return out


def _merge_resurrected_drops(start: list[dict], target: list[dict], merged: list[dict]) -> bool:
    """True when merge put back a row this writer intentionally dropped."""
    start_by = {r["id"]: r for r in start if r.get("id")}
    target_ids = {r.get("id") for r in target if r.get("id")}
    for row in merged:
        rid = row.get("id")
        if rid and rid in start_by and rid not in target_ids:
            return True
    return False


def _read_pending_index_writes(root: Path) -> list[dict]:
    path = root / "index.writes.jsonl"
    if not path.is_file():
        return []
    rows: list[dict] = []
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    except OSError:
        return []
    return rows


def _append_pending_index_write(root: Path, row: dict) -> None:
    path = root / "index.writes.jsonl"
    line = json.dumps(row, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as stream:
        stream.write(line.encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())


def _known_campaign_ids(root: Path, committed: list[dict], pending: list[dict]) -> set[str]:
    known: set[str] = set()
    for row in committed + pending:
        cid = row.get("campaign")
        if isinstance(cid, str) and cid:
            known.add(cid)
    camp_dir = root / "campaigns"
    if camp_dir.is_dir():
        for path in camp_dir.glob("*.json"):
            if _is_tmp(path.name):
                continue
            known.add(path.stem)
    return known


def _safe_cache_campaign_id(cid: str) -> bool:
    if not isinstance(cid, str) or not cid or cid in (".", ".."):
        return False
    if "/" in cid or "\\" in cid or "\0" in cid:
        return False
    return True


def _safe_unlink_campaign_file(root: Path, cid: str) -> None:
    if not _safe_cache_campaign_id(cid):
        return
    path = (root / "campaigns" / f"{cid}.json").resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return
    if path.is_file():
        path.unlink()


def _merge_index_writes(index_rows: list[dict], writes: list[dict], pruned) -> list[dict]:
    by: dict[str, dict] = {}
    for row in index_rows:
        cid = row.get("campaign")
        if not isinstance(cid, str) or not cid:
            continue
        by[cid] = row
    for row in writes:
        cid = row.get("campaign")
        if not isinstance(cid, str) or not cid:
            continue
        by[cid] = row
    for cid in pruned:
        if isinstance(cid, str):
            by.pop(cid, None)
    return _sort_index(list(by.values()))


def _pruned_campaign_ids(cfg, root: Path) -> set[str]:
    live = _live_campaign_ids(cfg)
    committed = _read_index(root)
    pending = _read_pending_index_writes(root)
    known = _known_campaign_ids(root, committed, pending)
    return known - live


def _write_version(root: Path) -> None:
    _atomic_write_bytes(root / "version", b"1\n")


def _persist_index_records(
    cfg,
    root: Path,
    *,
    records_at_start: list[dict],
    records: list[dict],
    index_at_start: list[dict],
    index_rows: list[dict],
    sources: dict | None = None,
) -> None:
    with _cache_write_guard(root):
        pruned = _pruned_campaign_ids(cfg, root)
        for cid in pruned:
            _safe_unlink_campaign_file(root, cid)
        disk_recs = _read_records(root)
        merged = _merge_record_writes(records_at_start, records, disk_recs)
        if _merge_resurrected_drops(records_at_start, records, merged):
            merged = records
        committed = _read_index(root)
        pending = _read_pending_index_writes(root)
        live = _live_campaign_ids(cfg)
        refresh_by = {r["campaign"]: r for r in index_rows if isinstance(r.get("campaign"), str) and r["campaign"] in live}
        merged_idx = _merge_index_writes(committed, pending + list(refresh_by.values()), pruned)
        _write_version(root)
        _write_records(root, merged)
        _write_index(root, merged_idx)
        writes_path = root / "index.writes.jsonl"
        if writes_path.is_file():
            writes_path.unlink()
        if sources is not None:
            sources["complete"] = True
            _atomic_write_json(root / "sources.json", sources)


def _recompute_campaign(cfg, cid, now, *, global_recs, runner_down, shares, window,
                        write_campaign: bool, extra_gaps: list[str] | None = None) -> tuple[dict, dict]:
    H = _lazy_history()
    doc = H._campaign_history(cfg, cid, now, shares, shared_global=global_recs, runner_down=runner_down)
    if extra_gaps:
        doc["gaps"] = sorted(set(doc.get("gaps") or []) | set(extra_gaps))
    ws = window or {}
    row = H._index_row_from_doc(doc)
    row["start"] = _iso_window(ws.get("start"))
    row["live_end"] = _iso_window(ws.get("live_end"))
    return doc, row


def _patch_index_windows(index_rows: list[dict], windows: dict[str, dict]) -> list[dict]:
    out = []
    for row in index_rows:
        win = windows.get(row.get("campaign") or "")
        if not win:
            out.append(row)
            continue
        patched = dict(row)
        patched["start"] = _iso_window(win.get("start"))  # JSON null when window is open-ended
        patched["live_end"] = _iso_window(win.get("live_end"))
        out.append(patched)
    return out


def _patch_one_window(index_rows: list[dict], cid: str, window: dict) -> list[dict]:
    return _patch_index_windows(index_rows, {cid: window or {}})


def _runner_log_snapshot(cfg, gaps: list[str]) -> tuple[list[tuple], int]:
    """One read of runner.jsonl for cold fill: intervals and committed byte offset."""
    H = _lazy_history()
    path = store.agentic_dir(cfg) / "runner.jsonl"
    if not path.is_file():
        gaps.append("runner.jsonl: missing")
        return [], 0
    out, down, last_stamp, saw_start = [], None, None, False
    committed = 0
    try:
        with path.open("rb") as stream:
            line = 0
            while True:
                pos = stream.tell()
                raw = stream.readline()
                if not raw:
                    committed = stream.tell()
                    break
                if not raw.endswith(b"\n"):
                    committed = pos
                    break
                line += 1
                if not raw.strip():
                    committed = stream.tell()
                    continue
                try:
                    row = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeError):
                    gaps.append(f"runner.jsonl: line {line} unparsable")
                    committed = stream.tell()
                    continue
                if not isinstance(row, dict):
                    committed = stream.tell()
                    continue
                stamp = H._parse_ts(row.get("ts"))
                if not stamp:
                    gaps.append(f"runner.jsonl: line {line} timestamp malformed")
                    committed = stream.tell()
                    continue
                kind = row.get("kind")
                if kind in ("stop", "crash"):
                    if down is None:
                        down = stamp
                elif kind == "start":
                    if down is not None and stamp > down:
                        out.append((down, stamp))
                    elif saw_start and last_stamp is not None and stamp > last_stamp:
                        out.append((last_stamp, stamp))
                    saw_start = True
                    down = None
                last_stamp = stamp
                committed = stream.tell()
    except OSError:
        gaps.append("runner.jsonl: missing")
        return [], 0
    return out, committed


def _parsed_log_offsets(cfg, *, exclude: frozenset[str] = frozenset()) -> dict[str, int]:
    agentic = store.agentic_dir(cfg)
    out: dict[str, int] = {}
    for key in (
        "conductor/decisions.jsonl",
        "conductor/sessions.jsonl",
        "spec-reviews.jsonl",
        "runner.jsonl",
    ):
        if key in exclude:
            continue
        path = agentic / key
        if path.is_file():
            out[key] = _complete_log_offset(path)
    mailbox = agentic / "mailbox"
    if mailbox.is_dir():
        for path in sorted(mailbox.glob("*.jsonl")):
            out[f"mailbox/{path.name}"] = _complete_log_offset(path)
    return out


def _stored_runner_down_s(root: Path, cid: str) -> int:
    doc = _read_json(root / "campaigns" / f"{cid}.json")
    if not isinstance(doc, dict):
        return 0
    return sum(int(ph.get("runner_down_s") or 0) for ph in doc.get("phases") or [])


def _write_campaign_file(root: Path, cid: str, doc: dict) -> None:
    out = doc["header"].get("outcome")
    camp = root / "campaigns" / f"{cid}.json"
    if out in _TERMINAL_OUTCOMES:
        _atomic_write_text(
            camp, json.dumps(doc, sort_keys=True, ensure_ascii=False) + "\n",
        )
    elif camp.is_file():
        camp.unlink()


def _cold_fill(cfg, root: Path, *, now=None, progress=None, _hold_guard: bool = False) -> dict:
    H = _lazy_history()
    CC = _lazy_conductor()
    now = H._aware(now or dt.datetime.now().astimezone())
    root.mkdir(parents=True, exist_ok=True)
    cids = _campaign_ids(cfg)
    gaps: list[str] = []
    runner_down, runner_off = _runner_log_snapshot(cfg, gaps)
    log_offsets: dict[str, int] = {}
    global_recs = H._global_records(cfg, cids, gaps, log_offsets=log_offsets)
    parsed_offsets = {"runner.jsonl": runner_off}
    parsed_offsets.update(log_offsets)
    sources = _scan_sources(cfg, fixed_offsets=parsed_offsets)
    sources["logs"] = _log_cursor_entries(cfg, parsed_offsets)
    windows = CC.live_windows(cfg, now=now)
    window_map = {w["campaign"]: w for w in windows}
    turn_cursor: dict = {}
    turns = _fresh_conductor_turns(cfg, cursor_out=turn_cursor)
    if turn_cursor.get("sessions"):
        sources.setdefault("logs", {})["conductor/sessions.jsonl"] = turn_cursor["sessions"]
    sources["transcripts"] = turn_cursor.get("transcripts") or {}
    shares = None
    if any(isinstance(t.get("usd"), (int, float)) for t in turns):
        shares, _ = CC._split(turns, windows)

    def _persist() -> dict:
        index_rows = []
        all_records: list[dict] = []
        headers = {}
        for done, cid in enumerate(cids, 1):
            doc, row = _recompute_campaign(
                cfg, cid, now, global_recs=global_recs, runner_down=runner_down, shares=shares,
                window=window_map.get(cid, {}), write_campaign=True, extra_gaps=gaps,
            )
            headers[cid] = doc["header"]
            index_rows.append(row)
            all_records.extend(_local_records(doc))
            _write_campaign_file(root, cid, doc)
            if progress:
                progress(done, len(cids))
        enriched_global = H._enrich_records(global_recs, headers)
        all_records.extend(enriched_global)
        H2 = _lazy_history()
        all_records = H2._enrich_records(all_records, headers)
        _write_version(root)
        _write_index(root, index_rows)
        _write_records(root, all_records)
        _atomic_write_json(root / "conductor_turns.json", {"turns": _serialize_turns(turns)})
        sources["global_gaps"] = sorted(set(gaps))
        sources["complete"] = True
        _atomic_write_json(root / "sources.json", sources)
        return {"campaigns": len(cids), "records": len(all_records)}

    if _hold_guard:
        return _persist()
    with _cache_write_guard(root):
        return _persist()


def _ensure_writable_cache(cfg, root: Path, *, now=None, progress=None) -> bool:
    sources = _read_json(root / "sources.json")
    if _sources_ok(sources) and (root / "index.jsonl").is_file() and (root / "records.jsonl").is_file():
        return False
    with _cache_write_guard(root):
        if root.exists():
            shutil.rmtree(root)
        _cold_fill(cfg, root, now=now, progress=progress, _hold_guard=True)
    return True


def _uncached_campaign(cfg, cid, now, cache, *, gap=False):
    H = _lazy_history()
    doc = H._campaign_history(cfg, cid, now, H._COMPUTE)
    if gap:
        gaps = list(doc.get("gaps") or [])
        gaps.append(_NOT_WRITABLE)
        doc["gaps"] = sorted(set(gaps))
    return doc


def _uncached_index(cfg, now, progress):
    return _lazy_history()._uncached_history_index(cfg, now=now, progress=progress)


def _uncached_records(cfg, **kwargs):
    return _lazy_history()._uncached_history_records(cfg, **kwargs)


def _doc_hit(cfg, root: Path, cid: str, now, sources: dict) -> dict | None:
    SC = _lazy_schemas()
    path = root / "campaigns" / f"{cid}.json"
    doc = _read_json(path)
    if not isinstance(doc, dict):
        return None
    if SC.history_problems(doc):
        return None
    if doc.get("signature") != _lazy_history().campaign_signature(cfg, cid):
        return None
    row = next((r for r in _read_index(root) if r.get("campaign") == cid), None)
    if not row or row.get("outcome") not in _TERMINAL_OUTCOMES:
        return None
    if "start" not in row:
        return None
    return doc


def _headers_for_enrich(index_rows: list[dict]) -> dict:
    return {r["campaign"]: r for r in index_rows if r.get("campaign")}


def _decision_records(H, cfg, cids: list[str], path: Path, gaps: list[str],
                       *, start: int = 0) -> list[dict]:
    out = []
    if not path.is_file():
        return out
    try:
        with path.open("rb") as stream:
            if start:
                stream.seek(start)
            line = 0
            while True:
                offset = stream.tell()
                raw = stream.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    break
                line += 1
                if not raw.strip():
                    continue
                try:
                    row = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeError):
                    gaps.append(f"decisions.jsonl: line {line} unparsable")
                    continue
                if not isinstance(row, dict):
                    continue
                rec = H._base_record("decisions", row.get("event_id") or offset, "decision", row)
                escalation, linked_by = H._single_escalation(row)
                if escalation:
                    rec["links"]["escalation"] = escalation
                    rec["linked_by"] = linked_by
                kind = H._campaign_text_link(rec, cids)
                if kind:
                    rec["linked_by"] = kind
                out.append(rec)
    except OSError:
        gaps.append("decisions.jsonl: missing")
    return out


def _runner_state_at_offset(path: Path, offset: int, gaps: list[str]) -> tuple:
    """Return (down, last_stamp, saw_start, position_after_last_complete_line)."""
    H = _lazy_history()
    down, last_stamp, saw_start = None, None, False
    if not path.is_file():
        return down, last_stamp, saw_start, 0
    try:
        with path.open("rb") as stream:
            line = 0
            committed = 0
            while stream.tell() < offset:
                pos = stream.tell()
                raw = stream.readline()
                if not raw:
                    committed = stream.tell()
                    break
                if not raw.endswith(b"\n"):
                    stream.seek(pos)
                    break
                line += 1
                committed = stream.tell()
                try:
                    row = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(row, dict):
                    continue
                stamp = H._parse_ts(row.get("ts"))
                if not stamp:
                    continue
                kind = row.get("kind")
                if kind in ("stop", "crash"):
                    if down is None:
                        down = stamp
                elif kind == "start":
                    if down is not None and stamp > down:
                        pass
                    saw_start = True
                    down = None
                last_stamp = stamp
            return down, last_stamp, saw_start, committed
    except OSError:
        return None, None, False, 0


def _runner_intervals_tail(path: Path, start: int, gaps: list[str],
                          down, last_stamp, saw_start) -> list[tuple]:
    H = _lazy_history()
    out = []
    if not path.is_file():
        return out
    try:
        with path.open("rb") as stream:
            stream.seek(start)
            while True:
                raw = stream.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    break
                try:
                    row = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(row, dict):
                    continue
                stamp = H._parse_ts(row.get("ts"))
                if not stamp:
                    continue
                kind = row.get("kind")
                if kind in ("stop", "crash"):
                    if down is None:
                        down = stamp
                elif kind == "start":
                    if down is not None and stamp > down:
                        out.append((down, stamp))
                    elif saw_start and last_stamp is not None and stamp > last_stamp:
                        out.append((last_stamp, stamp))
                    saw_start = True
                    down = None
                last_stamp = stamp
    except OSError:
        pass
    return out


def _prune_gone_campaigns(cfg, root: Path, index_rows: list[dict], records: list[dict],
                            *, persist: bool) -> tuple[list[dict], list[dict]]:
    index_ids = {r.get("campaign") for r in index_rows if r.get("campaign")}
    if not index_ids:
        return index_rows, records
    live = _live_campaign_ids(cfg)
    if not (index_ids - live):
        return index_rows, records
    changed = False
    for row in list(index_rows):
        cid = row.get("campaign")
        if cid and cid not in live:
            index_rows = [r for r in index_rows if r.get("campaign") != cid]
            records = _merge_records(records, drop_campaign_all=cid)
            _safe_unlink_campaign_file(root, cid)
            changed = True
    if changed:
        H = _lazy_history()
        live_list = sorted(live)
        gaps: list[str] = []
        global_recs = H._global_records(cfg, live_list, gaps)
        records = _merge_records(
            records, drop_predicate=lambda r: r.get("source") not in _LOCAL_SOURCES,
        )
        records.extend(H._enrich_records(global_recs, _headers_for_enrich(index_rows)))
        if persist:
            records_at_start = _read_records(root)
            index_at_start = _read_index(root)
            _persist_index_records(
                cfg,
                root,
                records_at_start=records_at_start,
                records=records,
                index_at_start=index_at_start,
                index_rows=index_rows,
            )
    return index_rows, records


def _replace_enriched_global(records: list[dict], index_rows: list[dict], global_recs: list[dict],
                             *, source: str | None = None, kind: str | None = None,
                             kinds: tuple[str, ...] | None = None) -> list[dict]:
    H = _lazy_history()
    headers = _headers_for_enrich(index_rows)
    if source is not None:
        records = _merge_records(records, drop_predicate=lambda r, s=source: r.get("source") == s)
        picks = [r for r in global_recs if r.get("source") == source]
    elif kind is not None:
        records = _merge_records(records, drop_predicate=lambda r, k=kind: r.get("kind") == k)
        picks = [r for r in global_recs if r.get("kind") == kind]
    elif kinds is not None:
        records = _merge_records(
            records, drop_predicate=lambda r, ks=kinds: r.get("kind") in ks,
        )
        picks = [r for r in global_recs if r.get("kind") in kinds]
    else:
        picks = []
    picks = H._enrich_records(picks, headers)
    records.extend(picks)
    return records


def _log_tail_incomplete(old: dict, new: dict) -> bool:
    """True when the file only grew with a trailing partial line (offset unchanged)."""
    if not old or not new:
        return False
    if (old.get("inode"), old.get("mtime_ns")) != (new.get("inode"), new.get("mtime_ns")):
        return False
    if int(new.get("size", 0)) <= int(old.get("size", 0)):
        return False
    return int(new.get("offset", 0)) <= int(old.get("offset", 0))


def _cursor_pending(stored: dict, fresh: dict) -> bool:
    old_logs = stored.get("logs") or {}
    new_logs = fresh.get("logs") or {}
    for key, old in old_logs.items():
        new = new_logs.get(key)
        if not new:
            continue
        if _log_tail_incomplete(old, new):
            continue
        if old.get("inode") == new.get("inode") and int(new.get("offset", 0)) > int(old.get("offset", 0)):
            return True
    return False


def _transcripts_need_refresh(cfg, stored: dict, fresh: dict) -> bool:
    """True when a transcript file grew or changed metadata since the published cursor."""
    old = stored.get("transcripts") or {}
    new = fresh.get("transcripts") or {}
    if set(old) != set(new):
        return True
    CC = _lazy_conductor()
    for row in CC.sessions(cfg):
        path = CC.find_transcript(cfg, row["session_id"])
        if path is None:
            continue
        resolved = str(path.resolve())
        prev_e = old.get(resolved)
        if not prev_e:
            return True
        try:
            st = path.stat()
        except OSError:
            return True
        if (prev_e.get("inode"), prev_e.get("mtime_ns")) != (st.st_ino, st.st_mtime_ns):
            return True
        if st.st_size > int(prev_e.get("size", 0)):
            return True
    return False


def _file_meta(entry: dict) -> tuple:
    return (entry.get("inode"), entry.get("size"), entry.get("mtime_ns"))


def _strip_global_input_gaps(gaps: list[str]) -> list[str]:
    prefixes = (
        "reviews/", "mailbox", "flaky-tests.json", "sentinel/", "escalations/",
        "escalation ", "spec-reviews.jsonl",
    )
    return [g for g in gaps if not any(g.startswith(p) for p in prefixes)]


def _global_cursor_unchanged(stored: dict, fresh: dict) -> bool:
    if (stored.get("files") or {}) != (fresh.get("files") or {}):
        return False
    if (stored.get("trees") or {}) != (fresh.get("trees") or {}):
        return False
    old_trans = stored.get("transcripts") or {}
    new_trans = fresh.get("transcripts") or {}
    if set(old_trans) != set(new_trans):
        return False
    for key in old_trans:
        if _file_meta(old_trans[key]) != _file_meta(new_trans.get(key) or {}):
            return False
    old_logs = stored.get("logs") or {}
    new_logs = fresh.get("logs") or {}
    if set(old_logs) != set(new_logs):
        return False
    for key, old in old_logs.items():
        new = new_logs.get(key) or {}
        if _log_tail_incomplete(old, new):
            continue
        if (old.get("inode"), old.get("size"), old.get("mtime_ns")) != (
                new.get("inode"), new.get("size"), new.get("mtime_ns")):
            return False
    return True


def _sync_globals(cfg, root: Path, now) -> set[str]:
    """Apply pending global log changes; return campaigns recomputed."""
    H = _lazy_history()
    now = H._aware(now or dt.datetime.now().astimezone())
    sources = _read_json(root / "sources.json")
    if not _sources_ok(sources):
        return set()
    fresh = _scan_sources(cfg, prev=sources)
    if _global_cursor_unchanged(sources, fresh) and not _cursor_pending(sources, fresh):
        return set()

    def _meta_changed(old, new):
        if old is None:
            return new is not None
        if new is None:
            return True
        return (old.get("inode"), old.get("size"), old.get("mtime_ns")) != (
            new.get("inode"), new.get("size"), new.get("mtime_ns"))

    affected: set[str] = set()
    agentic = store.agentic_dir(cfg)
    records = _read_records(root)
    records_at_start = list(records)
    index_rows = _read_index(root)
    index_at_start = list(index_rows)
    index_by = {r["campaign"]: r for r in index_rows}
    global_recs = None
    runner_down = None
    records_dirty = False
    runner_changed = False
    sessions_changed = False

    old_logs = sources.get("logs") or {}
    new_logs = fresh.get("logs") or {}
    cids = _campaign_ids(cfg)
    gaps: list[str] = list(sources.get("global_gaps") or [])
    prev_global_gaps = set(gaps)

    dec_path = agentic / "conductor" / "decisions.jsonl"
    old_dec = old_logs.get("conductor/decisions.jsonl")
    new_dec = new_logs.get("conductor/decisions.jsonl")
    dec_pending = (
        old_dec and new_dec
        and old_dec.get("inode") == new_dec.get("inode")
        and new_dec.get("size", 0) > old_dec.get("offset", 0)
    )
    if _meta_changed(old_dec, new_dec):
        old_dec_recs = [r for r in records if r.get("source") == "decisions"]
        old_ids = {r["id"] for r in old_dec_recs}
        old_by_id = {r["id"]: r for r in old_dec_recs}
        dec_gaps: list[str] = []
        new_decs = _decision_records(H, cfg, cids, dec_path, dec_gaps)
        gaps = [g for g in gaps if not g.startswith("decisions.jsonl")]
        gaps.extend(dec_gaps)
        new_decs = H._enrich_records(new_decs, _headers_for_enrich(index_rows))
        new_ids = {r["id"] for r in new_decs}
        records = _merge_records(records, drop_predicate=lambda r: r.get("source") == "decisions")
        records.extend(new_decs)
        records_dirty = True
        def _decision_changed(old: dict, new: dict) -> bool:
            for field in ("campaign", "ts", "title", "text"):
                if old.get(field) != new.get(field):
                    return True
            return (old.get("links") or {}) != (new.get("links") or {})

        new_by_id = {r["id"]: r for r in new_decs}
        for oid in new_ids | old_ids:
            prev = old_by_id.get(oid)
            new = new_by_id.get(oid)
            if prev and new:
                pc, nc = prev.get("campaign"), new.get("campaign")
                if pc and pc != nc:
                    affected.add(pc)
            if not new or not new.get("campaign"):
                continue
            if oid not in old_ids or (prev and _decision_changed(prev, new)):
                affected.add(new["campaign"])
        for oid in old_ids - new_ids:
            prev = old_by_id.get(oid)
            if prev and prev.get("campaign"):
                affected.add(prev["campaign"])
    elif dec_pending:
        start = int(old_dec.get("offset", 0))
        dec_gaps = []
        new_decs = _decision_records(H, cfg, cids, dec_path, dec_gaps, start=start)
        if new_decs:
            new_decs = H._enrich_records(new_decs, _headers_for_enrich(index_rows))
            new_ids = {r["id"] for r in new_decs}
            records = _merge_records(
                records,
                drop_predicate=lambda r, ids=new_ids: (
                    r.get("source") == "decisions" and r.get("id") in ids
                ),
            )
            records.extend(new_decs)
            records_dirty = True
            for row in new_decs:
                if row.get("campaign"):
                    affected.add(row["campaign"])
            entry = (fresh.get("logs") or {}).get("conductor/decisions.jsonl")
            if entry is not None and dec_path.is_file():
                entry["offset"] = _complete_log_offset(dec_path)
        gaps = [g for g in gaps if not g.startswith("decisions.jsonl")]
        gaps.extend(dec_gaps)

    run_path = agentic / "runner.jsonl"
    old_run = old_logs.get("runner.jsonl")
    new_run = new_logs.get("runner.jsonl")
    if _meta_changed(old_run, new_run):
        runner_changed = True
        gaps_run: list[str] = []
        all_iv = H._runner_intervals(cfg, gaps_run)
        runner_down = all_iv
        tail = (
            new_run and old_run
            and new_run.get("inode") == old_run.get("inode")
            and new_run.get("size", 0) > old_run.get("offset", 0)
            and new_run.get("size", 0) >= old_run.get("offset", 0)
        )
        if tail and run_path.is_file():
            state = _runner_state_at_offset(run_path, old_run.get("offset", 0), gaps_run)
            down, last_stamp, saw_start, committed = state
            new_iv = _runner_intervals_tail(
                run_path, committed, gaps_run, down, last_stamp, saw_start,
            )
            for row in index_rows:
                if row.get("outcome") not in _TERMINAL_OUTCOMES:
                    continue
                created = H._parse_ts(row.get("created_at"))
                closed = H._parse_ts(row.get("closed_at"))
                if not created or not closed:
                    continue
                for a, b in new_iv:
                    if a < closed and b > created:
                        affected.add(row["campaign"])
        else:
            for row in index_rows:
                if row.get("outcome") not in _TERMINAL_OUTCOMES:
                    continue
                cid = row["campaign"]
                created = H._parse_ts(row.get("created_at"))
                closed = H._parse_ts(row.get("closed_at"))
                if not created or not closed:
                    continue
                if _stored_runner_down_s(root, cid) > 0:
                    affected.add(cid)
                    continue
                if any(a < closed and b > created for a, b in all_iv):
                    affected.add(cid)
        gaps = [g for g in gaps if not g.startswith("runner.jsonl")]
        gaps.extend(gaps_run)

    needs_spec_review = (
        (sources.get("trees") or {}).get("reviews") != fresh.get("trees", {}).get("reviews")
        or _meta_changed(old_logs.get("spec-reviews.jsonl"), new_logs.get("spec-reviews.jsonl"))
    )
    mailbox_changed = False
    old_mail = {k: v for k, v in old_logs.items() if k.startswith("mailbox/")}
    new_mail = {k: v for k, v in new_logs.items() if k.startswith("mailbox/")}
    if set(old_mail) != set(new_mail):
        mailbox_changed = True
    else:
        for key in old_mail:
            if _meta_changed(old_mail[key], new_mail.get(key)):
                mailbox_changed = True
                break

    flaky_changed = _meta_changed(
        (sources.get("files") or {}).get("flaky-tests.json"),
        (fresh.get("files") or {}).get("flaky-tests.json"),
    )
    old_trees = sources.get("trees") or {}
    new_trees = fresh.get("trees") or {}
    esc_tree_changed = old_trees.get("escalations") != new_trees.get("escalations")
    sent_tree_changed = old_trees.get("sentinel") != new_trees.get("sentinel")
    esc_sent_tree_changed = esc_tree_changed or sent_tree_changed
    old_sess = old_logs.get("conductor/sessions.jsonl")
    new_sess = new_logs.get("conductor/sessions.jsonl")
    sess_incomplete = _log_tail_incomplete(old_sess, new_sess) if old_sess and new_sess else False
    sess_pending = (
        old_sess and new_sess
        and not sess_incomplete
        and old_sess.get("inode") == new_sess.get("inode")
        and int(new_sess.get("offset", 0)) > int(old_sess.get("offset", 0))
    )
    sessions_changed = (
        (_meta_changed(old_sess, new_sess) and not sess_incomplete)
        or sess_pending
        or _transcripts_need_refresh(cfg, sources, fresh)
    )

    need_global = needs_spec_review or mailbox_changed or flaky_changed or esc_sent_tree_changed
    if need_global:
        glob_gaps: list[str] = []
        global_recs = H._global_records(cfg, cids, glob_gaps)
        gaps = _strip_global_input_gaps(gaps)
        gaps.extend(glob_gaps)

    def _terminal_affected(campaigns: set[str]) -> None:
        for cid in campaigns:
            row = index_by.get(cid)
            if row and row.get("outcome") in _TERMINAL_OUTCOMES:
                affected.add(cid)

    if needs_spec_review and global_recs is not None:
        old_sr = {r["campaign"] for r in records if r.get("kind") == "spec_review" and r.get("campaign")}
        records = _replace_enriched_global(
            records, index_rows, global_recs, kind="spec_review",
        )
        records_dirty = True
        new_sr = {r["campaign"] for r in global_recs if r.get("kind") == "spec_review" and r.get("campaign")}
        _terminal_affected(old_sr | new_sr)

    if mailbox_changed and global_recs is not None:
        old_mail_c = {r["campaign"] for r in records if r.get("source") == "mailbox" and r.get("campaign")}
        records = _replace_enriched_global(records, index_rows, global_recs, source="mailbox")
        records_dirty = True
        new_mail_c = {r["campaign"] for r in global_recs if r.get("source") == "mailbox" and r.get("campaign")}
        _terminal_affected(old_mail_c | new_mail_c)

    if flaky_changed and global_recs is not None:
        old_fl = {r["campaign"] for r in records if r.get("source") == "flaky" and r.get("campaign")}
        records = _replace_enriched_global(records, index_rows, global_recs, source="flaky")
        records_dirty = True
        new_fl = {r["campaign"] for r in global_recs if r.get("source") == "flaky" and r.get("campaign")}
        _terminal_affected(old_fl | new_fl)

    if esc_sent_tree_changed and global_recs is not None:
        kinds: list[str] = []
        if esc_tree_changed:
            kinds.extend(("escalation", "answer"))
        if sent_tree_changed:
            kinds.append("sentinel")
        old_tree_c = {r["campaign"] for r in records if r.get("campaign") and r.get("kind") in kinds}
        records = _replace_enriched_global(records, index_rows, global_recs, kinds=tuple(kinds))
        records_dirty = True
        new_tree_c = {r["campaign"] for r in global_recs if r.get("campaign") and r.get("kind") in kinds}
        _terminal_affected(old_tree_c | new_tree_c)

    sessions_turns_dirty = False
    if sessions_changed:
        _load_turns(cfg, root, sources, snap=fresh, sources_out=fresh)
        sessions_turns_dirty = True

    live_ids = set(cids)
    index_rows, records = _prune_gone_campaigns(cfg, root, index_rows, records, persist=False)

    gaps_changed = set(gaps) != prev_global_gaps
    if gaps_changed:
        for row in index_rows:
            if row.get("outcome") in _TERMINAL_OUTCOMES:
                affected.add(row["campaign"])

    need_persist = (
        affected or records_dirty or runner_changed or sessions_changed or sessions_turns_dirty
        or gaps_changed
    )
    if need_persist:
        if global_recs is None:
            global_recs = H._global_records(cfg, cids, [])
        if runner_down is None:
            runner_down = H._runner_intervals(cfg, [])
        windows = {cid: _live_window_for_campaign(cfg, cid) for cid in affected if cid in live_ids}
        split_rows = _patch_index_windows(index_rows, windows)
        shares = _conductor_shares(cfg, root, split_rows, sources)
        for cid in sorted(affected):
            if cid not in live_ids:
                continue
            window = windows.get(cid) or index_by.get(cid, {})
            doc, row = _recompute_campaign(
                cfg, cid, now, global_recs=global_recs, runner_down=runner_down,
                shares=shares, window=window, write_campaign=True, extra_gaps=gaps,
            )
            index_rows = [row if r.get("campaign") == cid else r for r in index_rows]
            records = _merge_records(
                records, drop_campaign_all=cid, add=_records_for_campaign(doc, cid),
            )
            _write_campaign_file(root, cid, doc)
        if sessions_turns_dirty:
            turns = _load_turns(cfg, root, sources, snap=fresh, sources_out=fresh)
            _atomic_write_json(root / "conductor_turns.json", {"turns": _serialize_turns(turns)})
        fresh["global_gaps"] = sorted(set(gaps))
        _persist_index_records(
            cfg,
            root,
            records_at_start=records_at_start,
            records=records,
            index_at_start=index_at_start,
            index_rows=index_rows,
            sources=fresh,
        )
    elif sessions_turns_dirty:
        turns = _load_turns(cfg, root, sources, snap=fresh, sources_out=fresh)
        _atomic_write_json(root / "conductor_turns.json", {"turns": _serialize_turns(turns)})
        fresh["global_gaps"] = sorted(set(gaps))
        fresh["complete"] = True
        _atomic_write_json(root / "sources.json", fresh)
    return affected


def _refresh_live(cfg, root: Path, now, index_rows: list[dict], records: list[dict]) -> tuple:
    live_cids, missing = _live_refresh_targets(cfg, index_rows)
    if not live_cids and not missing:
        return index_rows, records
    records_at_start = list(records)
    index_at_start = list(index_rows)
    H = _lazy_history()
    gaps = []
    global_recs = H._global_records(cfg, _campaign_ids(cfg), gaps)
    runner_down = H._runner_intervals(cfg, gaps)
    sources = _read_json(root / "sources.json")
    touch = live_cids + missing
    windows = {cid: _live_window_for_campaign(cfg, cid) for cid in touch}
    split_rows = _patch_index_windows(index_rows, windows)
    shares = _conductor_shares(cfg, root, split_rows, sources)
    changed = False
    for cid in live_cids:
        window = windows[cid]
        doc, new_row = _recompute_campaign(
            cfg, cid, now, global_recs=global_recs, runner_down=runner_down, shares=shares,
            window=window, write_campaign=False, extra_gaps=gaps,
        )
        index_rows = [new_row if r.get("campaign") == cid else r for r in index_rows]
        records = _merge_records(
            records, drop_campaign_all=cid, add=_records_for_campaign(doc, cid),
        )
        _write_campaign_file(root, cid, doc)
        changed = True
    for cid in missing:
        window = windows[cid]
        doc, new_row = _recompute_campaign(
            cfg, cid, now, global_recs=global_recs, runner_down=runner_down, shares=shares,
            window=window, write_campaign=False, extra_gaps=gaps,
        )
        index_rows.append(new_row)
        records = _merge_records(
            records, drop_campaign_all=cid, add=_records_for_campaign(doc, cid),
        )
        _write_campaign_file(root, cid, doc)
        changed = True
    if changed:
        _persist_index_records(
            cfg,
            root,
            records_at_start=records_at_start,
            records=records,
            index_at_start=index_at_start,
            index_rows=index_rows,
        )
    return index_rows, records


def campaign_history(cfg, cid, *, now=None, cache=None) -> dict:
    H = _lazy_history()
    if store._contained_campaign_dir(cfg, cid) is None:
        raise ValueError(f"campaign {cid} not found")
    now = H._aware(now or dt.datetime.now().astimezone())
    root = _writable_root(cfg, cache)
    if root is None:
        return _uncached_campaign(cfg, cid, now, cache, gap=_not_writable(cfg, cache))
    _ensure_writable_cache(cfg, root, now=now)
    _sync_globals(cfg, root, now)
    sources = _read_json(root / "sources.json") or {}
    index_rows = _read_index(root)
    records = _read_records(root)
    index_rows, records = _prune_gone_campaigns(cfg, root, index_rows, records, persist=True)
    row = next((r for r in index_rows if r.get("campaign") == cid), None)
    if row and row.get("outcome") in _TERMINAL_OUTCOMES:
        hit = _doc_hit(cfg, root, cid, now, sources)
        sig = H.campaign_signature(cfg, cid)
        if hit is not None and hit.get("signature") == sig:
            return hit
    gaps = []
    global_recs = H._global_records(cfg, _campaign_ids(cfg), gaps)
    runner_down = H._runner_intervals(cfg, gaps)
    index_rows = _read_index(root)
    window = _live_window_for_campaign(cfg, cid)
    index_rows = _patch_one_window(index_rows, cid, window)
    shares = _conductor_shares(cfg, root, index_rows, sources)
    doc, new_row = _recompute_campaign(
        cfg, cid, now, global_recs=global_recs, runner_down=runner_down, shares=shares,
        window=window, write_campaign=True, extra_gaps=gaps,
    )
    records_start = list(_read_records(root))
    index_at_start = list(_read_index(root))
    records = _merge_records(
        records_start, drop_campaign_all=cid, add=_records_for_campaign(doc, cid),
    )
    index_rows = [new_row if r.get("campaign") == cid else r for r in index_rows]
    if not any(r.get("campaign") == cid for r in index_rows):
        index_rows.append(new_row)
    _write_campaign_file(root, cid, doc)
    _persist_index_records(
        cfg,
        root,
        records_at_start=records_start,
        records=records,
        index_at_start=index_at_start,
        index_rows=index_rows,
    )
    return doc


def history_index(cfg, *, now=None, progress=None, cache=None) -> list[dict]:
    H = _lazy_history()
    now = H._aware(now or dt.datetime.now().astimezone())
    root = _writable_root(cfg, cache)
    if root is None:
        return _uncached_index(cfg, now, progress)
    cold_filled = _ensure_writable_cache(cfg, root, now=now, progress=progress)
    if not cold_filled:
        _sync_globals(cfg, root, now)
    index_rows = _read_index(root)
    records = _read_records(root)
    index_rows, records = _prune_gone_campaigns(cfg, root, index_rows, records, persist=True)
    live_cids, missing = _live_refresh_targets(cfg, index_rows)
    if live_cids or missing:
        records = _read_records(root)
        index_rows, _ = _refresh_live(cfg, root, now, index_rows, records)
    sources = _read_json(root / "sources.json") or {}
    shares = _conductor_shares(cfg, root, index_rows, sources)
    _apply_conductor(index_rows, shares)
    if progress and not cold_filled:
        cids = _campaign_ids(cfg)
        for done, cid in enumerate(cids, 1):
            progress(done, len(cids))
    return [_public_index_row(row) for row in _sort_index(index_rows)]


def history_records(cfg, *, campaign=None, agent=None, kind=None, project=None,
                    severity=None, status=None, since=None, until=None, query=None,
                    limit=None, cache=None) -> list[dict]:
    H = _lazy_history()
    now = H._aware(dt.datetime.now().astimezone())
    root = _writable_root(cfg, cache)
    if root is None:
        return _uncached_records(
            cfg, campaign=campaign, agent=agent, kind=kind, project=project,
            severity=severity, status=status, since=since, until=until, query=query, limit=limit,
        )
    _ensure_writable_cache(cfg, root, now=now)
    _sync_globals(cfg, root, now)
    index_rows = _read_index(root)
    records = _read_records(root)
    index_rows, records = _prune_gone_campaigns(cfg, root, index_rows, records, persist=True)
    live_cids, missing = _live_refresh_targets(cfg, index_rows)
    kind_set = H._set(kind)
    needle = str(query or "").casefold()
    extra = any(x is not None for x in (campaign, agent, project, severity, status, since, until))
    stream_kinds = kind_set if kind_set and not extra else None
    stream_needle = needle if needle and not extra else ""
    if live_cids or missing:
        records = _read_records(root)
        index_rows, records = _refresh_live(cfg, root, now, index_rows, records)
    elif stream_kinds is not None or stream_needle:
        records = _read_records_filtered(root, kinds=stream_kinds, needle=stream_needle)
    else:
        records = _read_records(root)
    values = {name: H._set(value) for name, value in (
        ("campaign", campaign), ("agent", agent), ("kind", kind), ("project", project),
        ("severity", severity), ("status", status),
    )}
    since_b, until_b = H._date_bound(since), H._date_bound(until, until=True)
    out = []
    for rec in records:
        if any(wanted is not None and rec.get(name) not in wanted for name, wanted in values.items()):
            continue
        if not H._in_dates(rec.get("ts"), since_b, until_b):
            continue
        if needle and needle not in _search_text(rec):
            continue
        out.append(rec)
    out.sort(key=lambda rec: (H._time_key(rec.get("ts")), rec.get("id") or ""), reverse=True)
    return out[:limit] if limit is not None else out


def rebuild_cache(cfg, *, now=None, progress=None) -> dict:
    root = _writable_root(cfg, True)
    if root is None:
        raise OSError("cache: not writable")
    with _cache_write_guard(root):
        if root.exists():
            shutil.rmtree(root)
        return _cold_fill(cfg, root, now=now, progress=progress, _hold_guard=True)
