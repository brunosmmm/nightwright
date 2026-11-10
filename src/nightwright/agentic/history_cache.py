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
_cache_fallback = threading.local()


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


def note_cache_write_fallback() -> None:
    _cache_fallback.raised = True


def consume_cache_write_fallback() -> bool:
    if getattr(_cache_fallback, "raised", False):
        _cache_fallback.raised = False
        return True
    return False


def cache_wanted(cfg, cache) -> bool:
    """True when callers should use the cache path (cache=False never; True always; else cache_dir)."""
    if cache is False:
        return False
    if cache is True:
        return True
    cd = cfg.get("cache_dir")
    return isinstance(cd, str) and bool(cd.strip())


def cache_enabled(cfg, cache) -> bool:
    return cache_wanted(cfg, cache)


def _not_writable(cfg, cache) -> bool:
    if not cache_wanted(cfg, cache):
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
    return name.endswith(".tmp") or ".tmp-" in name


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
            if prev.get("mtime_ns") != row.get("mtime_ns"):
                row["offset"] = 0
            else:
                row["offset"] = prev.get("offset", row["size"])
        elif prev.get("inode") == row.get("inode") and row.get("size", 0) > prev.get("size", 0):
            row["offset"] = _complete_log_offset_from(path, int(prev.get("offset", 0)))
            if row["offset"] > 0 and "lines" in prev:
                row["lines"] = prev["lines"]
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


def _log_line_count(path: Path, *, up_to: int | None = None) -> int:
    if not path.is_file():
        return 0
    try:
        limit = up_to if up_to is not None else path.stat().st_size
        with path.open("rb") as stream:
            stream.seek(0)
            line = 0
            while stream.tell() < limit:
                raw = stream.readline()
                if not raw:
                    break
                if raw.endswith(b"\n"):
                    line += 1
                else:
                    break
            return line
    except OSError:
        return 0


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


def _tree_dirs_snapshot(root: Path) -> list:
    if not root.is_dir():
        return []
    parts: list[list] = []
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            try:
                parts.append([path.relative_to(root).as_posix(), path.stat().st_mtime_ns])
            except OSError:
                pass
    return parts


def _flat_dir_sig(
    root: Path, *, prefix: str | None = None, suffix: str | None = None,
) -> str:
    """One-level scandir hash for flat trees (escalations/, sentinel/)."""
    if not root.is_dir():
        return hashlib.sha1(b"").hexdigest()
    lines: list[str] = []
    try:
        with os.scandir(root) as it:
            for ent in sorted(it, key=lambda e: e.name):
                if not ent.is_file(follow_symlinks=False) or _is_tmp(ent.name):
                    continue
                if prefix is not None and not ent.name.startswith(prefix):
                    continue
                if suffix is not None and not ent.name.endswith(suffix):
                    continue
                try:
                    st = ent.stat(follow_symlinks=False)
                except OSError:
                    continue
                lines.append(f"{ent.name} {st.st_size} {st.st_ino} {st.st_mtime_ns}")
    except OSError:
        return hashlib.sha1(b"").hexdigest()
    return hashlib.sha1("\n".join(lines).encode("utf-8")).hexdigest()


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


def _agentic_tree_sig(agentic: Path, name: str, sub: str) -> str:
    root_path = agentic / sub
    if name == "escalations":
        return _flat_dir_sig(root_path, prefix="ESC-", suffix=".json")
    if name == "sentinel":
        return _flat_dir_sig(root_path, suffix=".last.json")
    return _tree_sig(root_path)


_LOGICAL_CACHE_NAMES = frozenset({
    "version", ".lock", "index.jsonl", "index.writes.jsonl", "records.jsonl", "sources.json",
})


def _campaign_file_cursors(cfg, cid: str) -> dict[str, dict]:
    camp = store.campaign_dir(cfg, cid)
    if camp is None:
        return {}
    agentic = store.agentic_dir(cfg)
    out: dict[str, dict] = {}
    for sub in ("runs", "gates"):
        base = camp / sub
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            rel = path.relative_to(agentic).as_posix()
            out[rel] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
    return out


def _campaign_rg_dirs_snapshot(cfg, cid: str) -> list | None:
    base = store.campaign_dir(cfg, cid)
    if base is None:
        return None
    return [list(part) for part in _runs_gates_dir_mtime_key(base)]


def _scan_all_campaign_files(cfg, cids: list[str]) -> dict[str, dict]:
    return {cid: _campaign_file_cursors(cfg, cid) for cid in cids}


def _scan_all_campaign_rg_dirs(cfg, cids: list[str]) -> dict[str, list]:
    out: dict[str, list] = {}
    for cid in cids:
        snap = _campaign_rg_dirs_snapshot(cfg, cid)
        if snap is not None:
            out[cid] = snap
    return out


def _set_campaign_cursors(sources: dict, cfg, cid: str) -> None:
    sources.setdefault("campaign_files", {})[cid] = _campaign_file_cursors(cfg, cid)
    snap = _campaign_rg_dirs_snapshot(cfg, cid)
    if snap is not None:
        sources.setdefault("campaign_rg_dirs", {})[cid] = snap


def _rg_dirs_key(stored_dirs) -> tuple | None:
    if not isinstance(stored_dirs, list):
        return None
    try:
        return tuple(tuple(part) for part in stored_dirs)
    except TypeError:
        return None


def _rg_dirs_live_matches(base: Path, stored_dirs) -> bool:
    """True when runs/gates directory mtimes match the stored snapshot (no file rglob)."""
    key = _rg_dirs_key(stored_dirs)
    if key is None:
        return False
    known = {rel: mtime for rel, mtime in key}
    found: set[str] = set()

    def walk_dirs(root: Path, prefix: str) -> bool:
        try:
            entries = list(root.iterdir())
        except OSError:
            return prefix in known
        for ent in entries:
            if not ent.is_dir():
                continue
            rel = f"{prefix}/{ent.name}"
            try:
                live_m = ent.stat().st_mtime_ns
            except OSError:
                return False
            if rel not in known or known[rel] != live_m:
                return False
            found.add(rel)
            if not walk_dirs(ent, rel):
                return False
        return True

    for sub in ("runs", "gates"):
        root = base / sub
        if not root.is_dir():
            if any(k == sub or k.startswith(f"{sub}/") for k in known):
                return False
            continue
        if not walk_dirs(root, sub):
            return False
    return found == set(known)


def _campaign_files_match(cfg, cid: str, stored: dict | None, rg_dirs=None) -> bool:
    return _campaign_stored_files_match(cfg, cid, stored, rg_dirs)


_RUNS_GATES_COUNT: dict[tuple, int] = {}


def _runs_gates_dir_mtime_key(base: Path) -> tuple:
    parts: list[tuple[str, int]] = []
    for sub in ("runs", "gates"):
        root = base / sub
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_dir():
                try:
                    parts.append((path.relative_to(base).as_posix(), path.stat().st_mtime_ns))
                except OSError:
                    pass
    return tuple(parts)


def _runs_gates_file_count(cfg, cid: str) -> int:
    base = store.campaign_dir(cfg, cid)
    if base is None:
        return 0
    try:
        key = (str(base.resolve()), _runs_gates_dir_mtime_key(base))
    except OSError:
        return 0
    hit = _RUNS_GATES_COUNT.get(key)
    if hit is not None:
        return hit
    agentic = store.agentic_dir(cfg)
    n = 0
    for sub in ("runs", "gates"):
        root = base / sub
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file():
                n += 1
    _RUNS_GATES_COUNT[key] = n
    return n


def _campaign_stored_files_match(cfg, cid: str, stored: dict | None, rg_dirs=None) -> bool:
    """Match stored runs/gates cursors; rglob only when directory mtimes disagree."""
    if not isinstance(stored, dict):
        return False
    base = store.campaign_dir(cfg, cid)
    if base is None:
        return not stored
    stored_key = _rg_dirs_key(rg_dirs)
    if stored_key is None:
        live = _campaign_file_cursors(cfg, cid)
        if live != stored:
            return False
    else:
        if not _rg_dirs_live_matches(base, rg_dirs):
            return _campaign_file_cursors(cfg, cid) == stored
    agentic = store.agentic_dir(cfg)
    for rel, cursor in stored.items():
        if not isinstance(cursor, dict):
            return False
        try:
            st = (agentic / rel).stat()
        except OSError:
            return False
        if st.st_size != cursor.get("size") or st.st_mtime_ns != cursor.get("mtime_ns"):
            return False
    return True


def _is_records_run_gates_path(rel: str) -> bool:
    if "/runs/" in rel:
        return rel.endswith("/run.json") or rel.endswith("/result.json")
    if "/gates/" in rel:
        return rel.endswith(".json")
    return False


def _campaign_records_run_inputs_match(cfg, cid: str, stored: dict | None) -> bool:
    """Stat only runs/<id>/run.json and result.json (records hit after a warm index)."""
    if not isinstance(stored, dict):
        return False
    agentic = store.agentic_dir(cfg)
    for rel, cursor in stored.items():
        if not isinstance(cursor, dict) or "/runs/" not in rel:
            continue
        if not (rel.endswith("/run.json") or rel.endswith("/result.json")):
            continue
        try:
            st = (agentic / rel).stat()
        except OSError:
            return False
        if st.st_size != cursor.get("size") or st.st_mtime_ns != cursor.get("mtime_ns"):
            return False
    return True


def _campaign_records_files_match(cfg, cid: str, stored: dict | None, rg_dirs=None) -> bool:
    """Runs/gates validity for history_records hit (no stat on stream/logs/request bodies)."""
    if not isinstance(stored, dict):
        return False
    base = store.campaign_dir(cfg, cid)
    if base is None:
        return not stored
    stored_key = _rg_dirs_key(rg_dirs)
    if stored_key is None:
        live = _campaign_file_cursors(cfg, cid)
        if live != stored:
            return False
    elif not _rg_dirs_live_matches(base, rg_dirs):
        return _campaign_file_cursors(cfg, cid) == stored
    agentic = store.agentic_dir(cfg)
    for rel, cursor in stored.items():
        if not isinstance(cursor, dict) or not _is_records_run_gates_path(rel):
            continue
        try:
            st = (agentic / rel).stat()
        except OSError:
            return False
        if st.st_size != cursor.get("size") or st.st_mtime_ns != cursor.get("mtime_ns"):
            return False
    return True


def _path_cursor(path: Path) -> dict | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "inode": st.st_ino}


def _cursor_matches(path: Path, cursor: dict) -> bool:
    live = _path_cursor(path)
    if live is None or not isinstance(cursor, dict):
        return False
    return (live.get("size"), live.get("mtime_ns"), live.get("inode")) == (
        cursor.get("size"), cursor.get("mtime_ns"), cursor.get("inode"),
    )


def _collect_v1_file_cursors(root: Path) -> dict:
    out: dict = {}
    for name in ("version", "index.jsonl", "records.jsonl"):
        path = root / name
        if path.is_file():
            cur = _path_cursor(path)
            if cur is not None:
                out[name] = cur
    camp = root / "campaigns"
    if camp.is_dir():
        for path in sorted(camp.glob("*.json")):
            if _is_tmp(path.name):
                continue
            cur = _path_cursor(path)
            if cur is not None:
                out[f"campaigns/{path.name}"] = cur
    return out


def _stamp_v1_files(sources: dict, root: Path) -> None:
    sources["v1_files"] = _collect_v1_file_cursors(root)


def _prices_canonical(cfg) -> str:
    cond = (cfg.get("agentic") or {}).get("conductor") if isinstance(cfg.get("agentic"), dict) else {}
    if not isinstance(cond, dict):
        cond = {}
    prices = cond.get("prices")
    if not isinstance(prices, dict):
        prices = {}
    return json.dumps(prices, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _profile_layer_cursors(cfg) -> dict[str, dict]:
    out: dict[str, dict] = {}
    from . import profiles as PF

    defs_dir = PF._DEFS_DIR
    if defs_dir.is_dir():
        for path in sorted(defs_dir.iterdir()):
            if not path.is_file() or path.suffix not in (".yaml", ".yml"):
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            out[f"builtin:{path.name}"] = {
                "size": st.st_size, "mtime_ns": st.st_mtime_ns, "inode": st.st_ino,
            }
    agentic = cfg.get("agentic") if isinstance(cfg.get("agentic"), dict) else {}
    profiles_delta = agentic.get("profiles")
    if isinstance(profiles_delta, dict):
        out["cfg:agentic.profiles"] = {
            "canonical": json.dumps(
                profiles_delta, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            ),
        }
    cfg_dir = cfg.get("config_dir")
    if cfg_dir:
        prof = Path(cfg_dir) / "profiles"
        if prof.is_dir():
            for path in sorted(prof.iterdir()):
                if not path.is_file():
                    continue
                try:
                    st = path.stat()
                except OSError:
                    continue
                out[str(path.resolve())] = {
                    "size": st.st_size, "mtime_ns": st.st_mtime_ns, "inode": st.st_ino,
                }
    project_root = cfg.get("project_root")
    if project_root:
        proj_prof = Path(project_root) / ".nw" / "profiles"
        if proj_prof.is_dir():
            for path in sorted(proj_prof.iterdir()):
                if not path.is_file():
                    continue
                try:
                    st = path.stat()
                except OSError:
                    continue
                out[str(path.resolve())] = {
                    "size": st.st_size, "mtime_ns": st.st_mtime_ns, "inode": st.st_ino,
                }
    return out


def _clear_v1_except_lock(root: Path) -> None:
    _invalidate_index_quiet_key()
    if not root.is_dir():
        return
    for child in list(root.iterdir()):
        if child.name == ".lock":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        elif child.is_file():
            child.unlink()


def _index_jsonl_parseable(path: Path) -> bool:
    if not path.is_file():
        return True
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            row = json.loads(raw)
            if not isinstance(row, dict):
                return False
    except (OSError, ValueError):
        return False
    return True


def _v1_cursors_match(root: Path, v1_files: dict) -> bool:
    if not isinstance(v1_files, dict):
        return False
    for rel, cursor in v1_files.items():
        if not isinstance(cursor, dict):
            return False
        if not _cursor_matches(root / rel, cursor):
            return False
    for name in ("version", "index.jsonl", "records.jsonl"):
        if name not in v1_files:
            return False
        if not (root / name).is_file():
            return False
    return True


def _v1_cursors_match_records_quiet(root: Path, v1_files: dict) -> bool:
    """Fingerprint-match arm: stat only the three stamped cache files, not campaigns/*.json."""
    if not isinstance(v1_files, dict):
        return False
    for name in ("version", "index.jsonl", "records.jsonl"):
        cursor = v1_files.get(name)
        if not isinstance(cursor, dict):
            return False
        if not (root / name).is_file():
            return False
        if not _cursor_matches(root / name, cursor):
            return False
    sources_path = root / "sources.json"
    src_key = "sources.json"
    if src_key in v1_files:
        cursor = v1_files[src_key]
        if not isinstance(cursor, dict) or not sources_path.is_file():
            return False
        if not _cursor_matches(sources_path, cursor):
            return False
    elif sources_path.is_file():
        if not _sources_ok(_read_json(sources_path)):
            return False
    return True


def _campaign_signature_from_root_scan(base: Path) -> str | None:
    ledger_size = ledger_mtime = None
    state_mtime = runs_mtime = gates_mtime = None
    try:
        with os.scandir(base) as it:
            for ent in it:
                if ent.is_file(follow_symlinks=False) and ent.name == "ledger.jsonl":
                    st = ent.stat(follow_symlinks=False)
                    ledger_size, ledger_mtime = st.st_size, st.st_mtime_ns
                elif ent.is_file(follow_symlinks=False) and ent.name == "state.json":
                    state_mtime = ent.stat(follow_symlinks=False).st_mtime_ns
                elif ent.is_dir(follow_symlinks=False) and ent.name == "runs":
                    runs_mtime = ent.stat(follow_symlinks=False).st_mtime_ns
                elif ent.is_dir(follow_symlinks=False) and ent.name == "gates":
                    gates_mtime = ent.stat(follow_symlinks=False).st_mtime_ns
    except OSError:
        return None
    values = (ledger_size, ledger_mtime, state_mtime, runs_mtime, gates_mtime)
    return hashlib.sha1(repr(values).encode("utf-8")).hexdigest()


def _rg_dirs_snapshot_scandir(base: Path) -> list[list] | None:
    parts: list[list] = []
    for sub in ("runs", "gates"):
        root = base / sub
        if not root.is_dir():
            continue
        stack: list[tuple[Path, str]] = [(root, sub)]
        while stack:
            cur, prefix = stack.pop()
            try:
                parts.append([prefix, cur.stat().st_mtime_ns])
            except OSError:
                return None
            try:
                with os.scandir(cur) as it:
                    for ent in it:
                        if ent.is_dir(follow_symlinks=False):
                            stack.append((Path(ent.path), f"{prefix}/{ent.name}"))
            except OSError:
                return None
    return parts


def _rg_dirs_norm_key(parts) -> tuple:
    if not isinstance(parts, list):
        return ()
    try:
        return tuple(tuple(p) for p in sorted(parts, key=lambda x: x[0]))
    except (TypeError, IndexError):
        return ()


def _rg_dirs_stored_match(base: Path, stored_dirs) -> bool:
    live = _rg_dirs_snapshot_scandir(base)
    if live is None:
        return False
    return _rg_dirs_norm_key(live) == _rg_dirs_norm_key(stored_dirs)


def _records_relevant_live_cursors(base: Path, agentic: Path) -> dict[str, dict] | None:
    out: dict[str, dict] = {}
    for sub in ("runs", "gates"):
        root = base / sub
        if not root.is_dir():
            continue
        stack: list[Path] = [root]
        while stack:
            cur = stack.pop()
            try:
                with os.scandir(cur) as it:
                    for ent in it:
                        if ent.is_dir(follow_symlinks=False):
                            stack.append(Path(ent.path))
                            continue
                        if not ent.is_file(follow_symlinks=False):
                            continue
                        name = ent.name
                        if sub == "runs":
                            if name not in ("run.json", "result.json"):
                                continue
                        elif not name.endswith(".json"):
                            continue
                        try:
                            st = ent.stat(follow_symlinks=False)
                        except OSError:
                            return None
                        rel = Path(ent.path).relative_to(agentic).as_posix()
                        out[rel] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
            except OSError:
                return None
    return out


def _stored_records_inputs_match(stored: dict, live: dict) -> bool:
    relevant = {k: v for k, v in stored.items() if _is_records_run_gates_path(k)}
    for rel, cursor in relevant.items():
        row = live.get(rel)
        if not isinstance(row, dict) or not isinstance(cursor, dict):
            return False
        if row.get("size") != cursor.get("size") or row.get("mtime_ns") != cursor.get("mtime_ns"):
            return False
    for rel in live:
        if rel not in relevant:
            return False
    return True


def _terminal_row_stale_records_quiet(cfg, cid: str, row: dict, sources: dict) -> bool:
    if row.get("outcome") not in _TERMINAL_OUTCOMES:
        return True
    stored_sig = row.get("signature")
    if not isinstance(stored_sig, str):
        return True
    stored_files = (sources.get("campaign_files") or {}).get(cid)
    if not isinstance(stored_files, dict):
        return True
    base = store.campaign_dir(cfg, cid)
    if base is None:
        return bool(stored_files)
    live_sig = _campaign_signature_from_root_scan(base)
    if live_sig is None or live_sig != stored_sig:
        return True
    rg_dirs = (sources.get("campaign_rg_dirs") or {}).get(cid)
    return not _campaign_records_files_match(cfg, cid, stored_files, rg_dirs)


def _cache_tree_corrupt(root: Path, sources: dict | None = None) -> bool:
    if not root.is_dir():
        return False
    if _index_writes_corrupt(root):
        return True
    has_logical = False
    for path in root.iterdir():
        if not path.is_file() or _is_tmp(path.name):
            continue
        name = path.name
        if name == "version":
            has_logical = True
            try:
                if path.read_bytes() != b"1\n":
                    return True
            except OSError:
                return True
            continue
        if name in _LOGICAL_CACHE_NAMES or name.endswith(".json"):
            has_logical = True
        elif (root / "campaigns").is_dir() and path.parent == root / "campaigns":
            has_logical = True
    ver = root / "version"
    if has_logical:
        try:
            if not ver.is_file() or ver.read_bytes() != b"1\n":
                return True
        except OSError:
            return True
    v1_files = (sources or {}).get("v1_files")
    fast = _sources_ok(sources) and isinstance(v1_files, dict) and (
        _v1_cursors_match_records_quiet(root, v1_files)
        or _v1_cursors_match(root, v1_files)
    )
    if fast:
        ver = root / "version"
        try:
            if not ver.is_file() or ver.read_bytes() != b"1\n":
                return True
        except OSError:
            return True
        for path in root.iterdir():
            if path.is_file() and _is_tmp(path.name):
                try:
                    path.unlink()
                except OSError:
                    pass
        camp = root / "campaigns"
        if camp.is_dir():
            for path in camp.iterdir():
                if path.is_file() and _is_tmp(path.name):
                    try:
                        path.unlink()
                    except OSError:
                        pass
            for path in camp.glob("*.json"):
                if _is_tmp(path.name):
                    continue
                key = f"campaigns/{path.name}"
                cur = (v1_files or {}).get(key) or {}
                if _cursor_matches(path, cur):
                    continue
                if _read_json(path) is None:
                    return True
        return False
    idx = root / "index.jsonl"
    if idx.is_file():
        if not (fast and _cursor_matches(idx, v1_files.get("index.jsonl") or {})):
            if not _index_jsonl_parseable(idx):
                return True
    elif fast:
        return True
    rec = root / "records.jsonl"
    if rec.is_file():
        if not (fast and _cursor_matches(rec, v1_files.get("records.jsonl") or {})):
            try:
                for raw in rec.read_text(encoding="utf-8").splitlines():
                    if not raw.strip():
                        continue
                    row = json.loads(raw)
                    if not isinstance(row, dict):
                        return True
            except (OSError, ValueError):
                return True
    elif fast:
        return True
    camp = root / "campaigns"
    if camp.is_dir():
        for path in camp.glob("*.json"):
            if _is_tmp(path.name):
                continue
            key = f"campaigns/{path.name}"
            if fast and key in v1_files and _cursor_matches(path, v1_files[key]):
                continue
            doc = _read_json(path)
            if doc is None:
                return True
    if sources is None:
        sources = _read_json(root / "sources.json")
    if (root / "sources.json").is_file() and not _sources_ok(sources):
        return True
    for path in root.iterdir():
        if path.is_file() and _is_tmp(path.name):
            try:
                path.unlink()
            except OSError:
                pass
    camp = root / "campaigns"
    if camp.is_dir():
        for path in camp.iterdir():
            if path.is_file() and _is_tmp(path.name):
                try:
                    path.unlink()
                except OSError:
                    pass
    return False


def _terminal_row_stale(cfg, cid: str, row: dict, sources: dict) -> bool:
    if row.get("outcome") not in _TERMINAL_OUTCOMES:
        return True
    H = _lazy_history()
    if row.get("signature") != H.campaign_signature(cfg, cid):
        return True
    stored_files = (sources.get("campaign_files") or {}).get(cid)
    if not isinstance(stored_files, dict):
        return True
    rg_dirs = (sources.get("campaign_rg_dirs") or {}).get(cid)
    if not _campaign_stored_files_match(cfg, cid, stored_files, rg_dirs):
        return True
    return False


def _stale_terminal_ids(cfg, index_rows: list[dict], sources: dict) -> set[str]:
    live = _live_campaign_ids(cfg)
    out: set[str] = set()
    for row in index_rows:
        cid = row.get("campaign")
        if not isinstance(cid, str) or cid not in live:
            continue
        if _terminal_row_stale(cfg, cid, row, sources):
            out.add(cid)
    return out


def _terminal_row_stale_for_records(cfg, cid: str, row: dict, sources: dict) -> bool:
    if row.get("outcome") not in _TERMINAL_OUTCOMES:
        return True
    H = _lazy_history()
    if row.get("signature") != H.campaign_signature(cfg, cid):
        return True
    stored_files = (sources.get("campaign_files") or {}).get(cid)
    if not isinstance(stored_files, dict):
        return True
    rg_dirs = (sources.get("campaign_rg_dirs") or {}).get(cid)
    if not _campaign_records_files_match(cfg, cid, stored_files, rg_dirs):
        return True
    return False


def _stale_terminal_ids_for_records(cfg, index_rows: list[dict], sources: dict) -> set[str]:
    live = _live_campaign_ids(cfg)
    out: set[str] = set()
    for row in index_rows:
        cid = row.get("campaign")
        if not isinstance(cid, str) or cid not in live:
            continue
        if _terminal_row_stale_for_records(cfg, cid, row, sources):
            out.add(cid)
    return out


def _terminal_row_stale_run_inputs_only(cfg, cid: str, row: dict, sources: dict) -> bool:
    if row.get("outcome") not in _TERMINAL_OUTCOMES:
        return True
    H = _lazy_history()
    if row.get("signature") != H.campaign_signature(cfg, cid):
        return True
    stored_files = (sources.get("campaign_files") or {}).get(cid)
    if not isinstance(stored_files, dict):
        return True
    rg_dirs = (sources.get("campaign_rg_dirs") or {}).get(cid)
    if not _campaign_records_files_match(cfg, cid, stored_files, rg_dirs):
        return True
    return False


def _stale_terminal_ids_run_inputs_only(cfg, index_rows: list[dict], sources: dict) -> set[str]:
    live = _live_campaign_ids(cfg)
    out: set[str] = set()
    for row in index_rows:
        cid = row.get("campaign")
        if not isinstance(cid, str) or cid not in live:
            continue
        if _terminal_row_stale_run_inputs_only(cfg, cid, row, sources):
            out.add(cid)
    return out


def _stale_terminal_ids_records_quiet(cfg, index_rows: list[dict], sources: dict) -> set[str]:
    live = _live_campaign_ids(cfg)
    out: set[str] = set()
    for row in index_rows:
        cid = row.get("campaign")
        if not isinstance(cid, str) or cid not in live:
            continue
        if _terminal_row_stale_records_quiet(cfg, cid, row, sources):
            out.add(cid)
    return out


def _recompute_terminal_documents(
    cfg, root: Path, now, index_rows: list[dict], records: list[dict], cids: set[str],
) -> tuple[list[dict], list[dict], dict | None]:
    if not cids:
        return index_rows, records, None
    H = _lazy_history()
    gaps: list[str] = []
    global_recs = H._global_records(cfg, _campaign_ids(cfg), gaps)
    runner_down = H._runner_intervals(cfg, gaps)
    sources = _read_json(root / "sources.json") or {}
    index_by = {r["campaign"]: r for r in index_rows}
    windows = {cid: _live_window_for_campaign(cfg, cid) for cid in cids}
    split_rows = _patch_index_windows(index_rows, windows)
    shares = _split_conductor_shares(cfg, root, split_rows, sources)
    for cid in sorted(cids):
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
        _append_pending_index_write(root, row)
    fresh_sources = _read_json(root / "sources.json") or sources
    for cid in cids:
        _set_campaign_cursors(fresh_sources, cfg, cid)
    return index_rows, records, fresh_sources


def _search_text(rec: dict) -> str:
    return f"{rec.get('title') or ''} {rec.get('text') or ''}".casefold()


def _stored_line(rec: dict) -> str:
    return json.dumps({**rec, "search": _search_text(rec)}, ensure_ascii=False)


_records_mem: dict[tuple, dict] = {}
_CACHE_WRITE_LOCK = threading.Lock()
_INDEX_QUIET_KEY: tuple | None = None


def _invalidate_index_quiet_key() -> None:
    global _INDEX_QUIET_KEY
    _INDEX_QUIET_KEY = None


def _index_quiet_fingerprint(
    cfg, root: Path, sources: dict, index_rows: list[dict], live: set[str],
) -> tuple:
    v1 = sources.get("v1_files") or {}
    v1_sig = tuple(
        sorted(
            (name, c.get("inode"), c.get("size"), c.get("mtime_ns"))
            for name, c in v1.items()
            if isinstance(c, dict)
        )
    )
    return (str(root.resolve()), v1_sig, frozenset(live), len(index_rows))


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
        by_kind: dict[str, list[int]] = {}
        for idx, (rec, _) in enumerate(entries):
            kind = rec.get("kind")
            if isinstance(kind, str):
                by_kind.setdefault(kind, []).append(idx)
        _records_mem[key] = {"entries": entries, "by_kind": by_kind}


def _get_records_mem(root: Path) -> dict | None:
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
        return [rec for rec, _ in cached["entries"]]
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
        entries = cached["entries"]
        if kinds is not None and not needle and len(kinds) == 1:
            idxs = cached.get("by_kind", {}).get(next(iter(kinds)))
            if idxs is not None:
                return [entries[i][0] for i in idxs]
        out = []
        for rec, search in entries:
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


def _records_sort_key(rec: dict) -> tuple:
    H = _lazy_history()
    return (H._time_key(rec.get("ts")), rec.get("id") or "")


def _sort_records_newest_first(records: list[dict]) -> list[dict]:
    return sorted(records, key=_records_sort_key, reverse=True)


def _write_records(root: Path, records: list[dict]) -> None:
    records = _sort_records_newest_first(records)
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
            if prev_log and (
                (prev_log.get("inode"), prev_log.get("size"), prev_log.get("mtime_ns"))
                == (entry.get("inode"), entry.get("size"), entry.get("mtime_ns"))
            ):
                if "lines" in prev_log:
                    entry["lines"] = prev_log["lines"]
            logs[key] = entry
    mailbox = agentic / "mailbox"
    if mailbox.is_dir():
        for path in sorted(mailbox.glob("*.jsonl")):
            key = f"mailbox/{path.name}"
            prev_log = prev_logs.get(key)
            if fixed_offsets is not None and key in fixed_offsets:
                entry = _file_entry(path)
                if entry is not None:
                    entry["offset"] = min(fixed_offsets[key], int(entry["size"]))
                    logs[key] = entry
                continue
            entry = _log_entry(path, prev=prev_log)
            if entry is not None:
                if prev_log and (
                    (prev_log.get("inode"), prev_log.get("size"), prev_log.get("mtime_ns"))
                    == (entry.get("inode"), entry.get("size"), entry.get("mtime_ns"))
                ):
                    if "lines" in prev_log:
                        entry["lines"] = prev_log["lines"]
                logs[key] = entry
    files = {}
    flaky = agentic / "flaky-tests.json"
    entry = _file_entry(flaky)
    if entry is not None:
        files["flaky-tests.json"] = entry
    prev_trees = (prev or {}).get("trees") or {}
    prev_tree_dirs = (prev or {}).get("tree_dirs") or {}
    trees = {}
    tree_dirs: dict[str, list] = {}
    for name, sub in (
        ("escalations", "escalations"), ("reviews", "reviews"), ("sentinel", "sentinel"),
    ):
        root_path = agentic / sub
        snap = _tree_dirs_snapshot(root_path)
        tree_dirs[name] = snap
        if name in ("escalations", "sentinel"):
            trees[name] = _agentic_tree_sig(agentic, name, sub)
        else:
            trees[name] = _tree_sig(root_path)
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
    out = {
        "version": 1,
        "complete": False,
        "logs": logs,
        "files": files,
        "trees": trees,
        "tree_dirs": tree_dirs,
        "transcripts": transcripts,
        "prices": _prices_canonical(cfg),
        "profile_layers": _profile_layer_cursors(cfg),
    }
    prev_cfiles = (prev or {}).get("campaign_files")
    if isinstance(prev_cfiles, dict):
        out["campaign_files"] = dict(prev_cfiles)
    prev_rg = (prev or {}).get("campaign_rg_dirs")
    if isinstance(prev_rg, dict):
        out["campaign_rg_dirs"] = dict(prev_rg)
    return out


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


def _log_cursor_entries(
    cfg,
    parsed_offsets: dict[str, int],
    *,
    line_counts: dict[str, int] | None = None,
) -> dict[str, dict]:
    """Log cursors for a cold fill: never publish size past bytes parsed at fill time."""
    agentic = store.agentic_dir(cfg)
    logs: dict[str, dict] = {}
    line_counts = line_counts or {}
    for key, off in parsed_offsets.items():
        path = agentic / key
        if not path.is_file():
            continue
        entry = _file_entry(path)
        if entry is None:
            continue
        entry["offset"] = off
        if key in line_counts:
            entry["lines"] = line_counts[key]
        else:
            entry["lines"] = _log_line_count(path, up_to=off)
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


def _conductor_cursors_unchanged(cfg, sources: dict | None, snap: dict | None = None) -> bool:
    if not sources:
        return False
    fresh = snap or _scan_sources(cfg, prev=sources)
    if sources.get("prices") != fresh.get("prices"):
        return False
    if sources.get("profile_layers") != fresh.get("profile_layers"):
        return False
    if _transcripts_need_refresh(cfg, sources, fresh):
        return False
    old_sess = (sources.get("logs") or {}).get("conductor/sessions.jsonl")
    new_sess = (fresh.get("logs") or {}).get("conductor/sessions.jsonl")
    return _file_meta(old_sess or {}) == _file_meta(new_sess or {})


def _load_turns(
    cfg,
    root: Path,
    sources: dict | None,
    *,
    snap: dict | None = None,
    sources_out: dict | None = None,
):
    snap = snap or _scan_sources(cfg, prev=sources)
    turn_cursor: dict = {}
    turns = _fresh_conductor_turns(cfg, cursor_out=turn_cursor)
    if sources_out is not None:
        _apply_turn_cursors(sources_out, turn_cursor)
        sources_out["prices"] = snap.get("prices")
        sources_out["profile_layers"] = snap.get("profile_layers")
    return turns


def _conductor_shares(cfg, root: Path, index_rows, sources: dict | None) -> dict | None:
    if _conductor_cursors_unchanged(cfg, sources):
        return None
    CC = _lazy_conductor()
    turns = _load_turns(cfg, root, sources)
    if not turns or not any(isinstance(t.get("usd"), (int, float)) for t in turns):
        return None
    windows = _window_rows(index_rows)
    shares, _ = CC._split(turns, windows)
    return shares


def _split_conductor_shares(cfg, root: Path, index_rows, sources: dict | None) -> dict | None:
    """Live conductor attribution for documents that are being rewritten."""
    CC = _lazy_conductor()
    turns = _load_turns(cfg, root, sources)
    if not turns or not any(isinstance(t.get("usd"), (int, float)) for t in turns):
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


def _index_writes_corrupt(root: Path) -> bool:
    path = root / "index.writes.jsonl"
    if not path.is_file():
        return False
    try:
        data = path.read_bytes()
    except OSError:
        return True
    if not data:
        return False
    parts = data.split(b"\n")
    if not data.endswith(b"\n"):
        parts = parts[:-1]
    for raw in parts:
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            return True
        if not isinstance(row, dict):
            return True
    return False


def _read_pending_index_writes(root: Path) -> list[dict]:
    path = root / "index.writes.jsonl"
    if not path.is_file():
        return []
    rows: list[dict] = []
    try:
        data = path.read_bytes()
    except OSError:
        return []
    if not data:
        return []
    parts = data.split(b"\n")
    if not data.endswith(b"\n"):
        parts = parts[:-1]
    for raw in parts:
        if not raw.strip():
            continue
        try:
            row = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            continue
        if isinstance(row, dict):
            rows.append(row)
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


def _apply_pending_index_writes(cfg, root: Path, *, _hold_guard: bool = False) -> None:
    writes_path = root / "index.writes.jsonl"
    if not writes_path.is_file():
        return

    def _do() -> None:
        pruned = _pruned_campaign_ids(cfg, root)
        for cid in pruned:
            _safe_unlink_campaign_file(root, cid)
        committed = _read_index(root)
        pending = _read_pending_index_writes(root)
        merged_idx = _merge_index_writes(committed, pending, pruned)
        records_changed = False
        records = _read_records(root)
        if pruned:
            H = _lazy_history()
            records = _merge_records(
                records,
                drop_predicate=lambda r, p=pruned: (
                    r.get("campaign") in p and r.get("source") in _LOCAL_SOURCES
                ),
            )
            live_list = sorted(_live_campaign_ids(cfg))
            gaps: list[str] = []
            global_recs = H._global_records(cfg, live_list, gaps)
            records = _merge_records(
                records, drop_predicate=lambda r: r.get("source") not in _LOCAL_SOURCES,
            )
            records.extend(H._enrich_records(global_recs, _headers_for_enrich(merged_idx)))
            records_changed = True
        _write_index(root, merged_idx)
        if records_changed:
            _write_records(root, records)
        writes_path.unlink()

    if _hold_guard:
        _do()
    else:
        with _cache_write_guard(root):
            _do()


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
    _hold_guard: bool = False,
) -> None:
    _invalidate_index_quiet_key()
    _RUNS_GATES_COUNT.clear()

    def _do() -> None:
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
            _stamp_v1_files(sources, root)
            _atomic_write_json(root / "sources.json", sources)

    if _hold_guard:
        _do()
    else:
        with _cache_write_guard(root):
            _do()


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
        return [], 0, 0
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
        return [], 0, 0
    return out, committed, line


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
    runner_down, runner_off, runner_lines = _runner_log_snapshot(cfg, gaps)
    log_offsets: dict[str, int] = {}
    global_recs = H._global_records(cfg, cids, gaps, log_offsets=log_offsets)
    parsed_offsets = {"runner.jsonl": runner_off}
    parsed_offsets.update(log_offsets)
    sources = _scan_sources(cfg, fixed_offsets=parsed_offsets)
    sources["logs"] = _log_cursor_entries(
        cfg, parsed_offsets, line_counts={"runner.jsonl": runner_lines},
    )
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
        _write_version(root)
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
            _append_pending_index_write(root, row)
            if progress:
                progress(done, len(cids))
        enriched_global = H._enrich_records(global_recs, headers)
        all_records.extend(enriched_global)
        H2 = _lazy_history()
        all_records = H2._enrich_records(all_records, headers)
        pending = _read_pending_index_writes(root)
        merged_idx = _merge_index_writes([], pending, set())
        _write_index(root, merged_idx)
        writes_path = root / "index.writes.jsonl"
        if writes_path.is_file():
            writes_path.unlink()
        _write_records(root, all_records)
        sources["campaign_files"] = _scan_all_campaign_files(cfg, cids)
        sources["campaign_rg_dirs"] = _scan_all_campaign_rg_dirs(cfg, cids)
        sources["global_gaps"] = sorted(set(gaps))
        sources["complete"] = True
        _stamp_v1_files(sources, root)
        _atomic_write_json(root / "sources.json", sources)
        return {"campaigns": len(cids), "records": len(all_records)}

    if _hold_guard:
        return _persist()
    with _cache_write_guard(root):
        return _persist()


def _ensure_writable_cache(cfg, root: Path, *, now=None, progress=None) -> bool:
    sources_peek = _read_json(root / "sources.json")
    if (
        _sources_ok(sources_peek)
        and (root / "version").is_file()
        and (root / "index.jsonl").is_file()
        and (root / "records.jsonl").is_file()
        and not (root / "index.writes.jsonl").is_file()
    ):
        v1_peek = (sources_peek or {}).get("v1_files") or {}
        if isinstance(v1_peek, dict) and _v1_cursors_match_records_quiet(root, v1_peek):
            if not _cache_tree_corrupt(root, sources_peek):
                return False
    if _cache_tree_corrupt(root, sources_peek):
        with _cache_write_guard(root):
            _clear_v1_except_lock(root)
            _cold_fill(cfg, root, now=now, progress=progress, _hold_guard=True)
        return True
    if (
        _sources_ok(sources_peek)
        and (root / "version").is_file()
        and (root / "index.jsonl").is_file()
        and (root / "records.jsonl").is_file()
    ):
        v1 = (sources_peek or {}).get("v1_files") or {}
        if isinstance(v1, dict) and _v1_cursors_match_records_quiet(root, v1):
            return False
    sources = _read_json(root / "sources.json")
    if _sources_ok(sources) and (root / "index.jsonl").is_file() and (root / "records.jsonl").is_file():
        return False
    with _cache_write_guard(root):
        _clear_v1_except_lock(root)
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
    stored_files = (sources.get("campaign_files") or {}).get(cid)
    if not isinstance(stored_files, dict):
        return None
    rg_dirs = (sources.get("campaign_rg_dirs") or {}).get(cid)
    if not _campaign_files_match(cfg, cid, stored_files, rg_dirs):
        return None
    return doc


def _headers_for_enrich(index_rows: list[dict]) -> dict:
    return {r["campaign"]: r for r in index_rows if r.get("campaign")}


def _decision_records(H, cfg, cids: list[str], path: Path, gaps: list[str],
                       *, start: int = 0, line_start: int = 0) -> tuple[list[dict], int, int]:
    out: list[dict] = []
    line = line_start
    end_offset = start
    if not path.is_file():
        gaps.append("decisions.jsonl: missing")
        return out, line, end_offset
    try:
        with path.open("rb") as stream:
            if start:
                stream.seek(start)
            while True:
                pos = stream.tell()
                raw = stream.readline()
                if not raw:
                    end_offset = stream.tell()
                    break
                if not raw.endswith(b"\n"):
                    stream.seek(pos)
                    end_offset = pos
                    break
                line += 1
                end_offset = stream.tell()
                if not raw.strip():
                    continue
                try:
                    row = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeError):
                    gaps.append(f"decisions.jsonl: line {line} unparsable")
                    continue
                if not isinstance(row, dict):
                    gaps.append(f"decisions.jsonl: line {line} unparsable")
                    continue
                rec = H._base_record("decisions", row.get("event_id") or pos, "decision", row)
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
    return out, line, end_offset


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
                            *, persist: bool, _hold_guard: bool = False) -> tuple[list[dict], list[dict]]:
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
            now = H._aware(dt.datetime.now().astimezone())
            survivors = {
                r["campaign"] for r in index_rows
                if r.get("outcome") in _TERMINAL_OUTCOMES and r.get("campaign") in live
            }
            sources_out = None
            if survivors:
                touch: set[str] = set()
                for cid in survivors:
                    doc = _read_json(root / "campaigns" / f"{cid}.json")
                    if not isinstance(doc, dict):
                        touch.add(cid)
                        continue
                    pure = H.campaign_history(cfg, cid, now=now, cache=False)
                    a = {k: v for k, v in doc.items() if k != "generated_at"}
                    b = {k: v for k, v in pure.items() if k != "generated_at"}
                    if a != b:
                        touch.add(cid)
                if touch:
                    index_rows, records, sources_out = _recompute_terminal_documents(
                        cfg, root, now, index_rows, records, touch,
                    )
            records_at_start = _read_records(root)
            index_at_start = _read_index(root)
            _persist_index_records(
                cfg,
                root,
                records_at_start=records_at_start,
                records=records,
                index_at_start=index_at_start,
                index_rows=index_rows,
                sources=sources_out,
                _hold_guard=_hold_guard,
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
    if old.get("inode") != new.get("inode"):
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


def _sources_unchanged_light(cfg, stored: dict) -> bool:
    if stored.get("prices") != _prices_canonical(cfg):
        return False
    if stored.get("profile_layers") != _profile_layer_cursors(cfg):
        return False
    agentic = store.agentic_dir(cfg)
    stored_logs = stored.get("logs") or {}
    for key in (
        "conductor/decisions.jsonl",
        "conductor/sessions.jsonl",
        "spec-reviews.jsonl",
        "runner.jsonl",
    ):
        if (agentic / key).is_file() and key not in stored_logs:
            return False
    flaky = agentic / "flaky-tests.json"
    if flaky.is_file() and "flaky-tests.json" not in (stored.get("files") or {}):
        return False
    CC = _lazy_conductor()
    stored_trans = stored.get("transcripts") or {}
    for row in CC.sessions(cfg):
        path = CC.find_transcript(cfg, row["session_id"])
        if path is not None:
            resolved = str(path.resolve())
            if resolved not in stored_trans:
                return False
    for key, old in (stored.get("logs") or {}).items():
        path = agentic / key
        entry = _log_entry(path, prev=old)
        if entry is None:
            if old:
                return False
            continue
        if _log_tail_incomplete(old, entry):
            continue
        if (old.get("inode"), old.get("size"), old.get("mtime_ns")) != (
                entry.get("inode"), entry.get("size"), entry.get("mtime_ns")):
            return False
        if (
            old.get("inode") == entry.get("inode")
            and int(entry.get("size", 0)) > int(old.get("size", 0))
            and int(entry.get("offset", 0)) > int(old.get("offset", 0))
        ):
            return False
    mailbox = agentic / "mailbox"
    if mailbox.is_dir():
        stored_logs = stored.get("logs") or {}
        for path in sorted(mailbox.glob("*.jsonl")):
            key = f"mailbox/{path.name}"
            if key not in stored_logs:
                return False
    for key, old in (stored.get("files") or {}).items():
        entry = _file_entry(agentic / key)
        if _file_meta(old) != _file_meta(entry or {}):
            return False
    prev_tree_dirs = stored.get("tree_dirs") or {}
    prev_trees = stored.get("trees") or {}
    for name, sub in (
        ("escalations", "escalations"), ("reviews", "reviews"), ("sentinel", "sentinel"),
    ):
        root_path = agentic / sub
        if name in ("escalations", "sentinel"):
            if prev_trees.get(name) != _agentic_tree_sig(agentic, name, sub):
                return False
        elif name == "reviews":
            if prev_trees.get(name) != _tree_sig(root_path):
                return False
        elif _tree_dirs_snapshot(root_path) != prev_tree_dirs.get(name):
            return False
    for resolved, old in (stored.get("transcripts") or {}).items():
        entry = _file_entry(Path(resolved))
        if _file_meta(old) != _file_meta(entry or {}):
            return False
    return True


def _cache_refresh_quiet(cfg, root: Path, sources: dict) -> bool:
    if not _sources_ok(sources):
        return False
    if (root / "index.writes.jsonl").is_file():
        return False
    if not _sources_unchanged_light(cfg, sources):
        return False
    v1_files = sources.get("v1_files") or {}
    if not isinstance(v1_files, dict) or not _v1_cursors_match_records_quiet(root, v1_files):
        return False
    index_rows = _read_index(root)
    live = _live_campaign_ids(cfg)
    if {r.get("campaign") for r in index_rows if r.get("campaign")} != live:
        return False
    if _stale_terminal_ids_run_inputs_only(cfg, index_rows, sources):
        return False
    live_cids, missing = _live_refresh_targets(cfg, index_rows)
    if live_cids or missing:
        return False
    global _INDEX_QUIET_KEY
    _INDEX_QUIET_KEY = _index_quiet_fingerprint(cfg, root, sources, index_rows, live)
    return True


def _records_cache_quiet(cfg, root: Path, sources: dict) -> bool:
    if not _sources_ok(sources):
        return False
    if (root / "index.writes.jsonl").is_file():
        return False
    v1_files = sources.get("v1_files") or {}
    if not isinstance(v1_files, dict):
        return False
    index_rows = _read_index(root)
    live = _live_campaign_ids(cfg)
    if {r.get("campaign") for r in index_rows if r.get("campaign")} != live:
        return False
    fp = _index_quiet_fingerprint(cfg, root, sources, index_rows, live)
    if fp == _INDEX_QUIET_KEY:
        if not _sources_unchanged_light(cfg, sources):
            return False
        if not _v1_cursors_match_records_quiet(root, v1_files):
            return False
        if _stale_terminal_ids_for_records(cfg, index_rows, sources):
            return False
        live_cids, missing = _live_refresh_targets(cfg, index_rows)
        return not live_cids and not missing
    else:
        if not _sources_unchanged_light(cfg, sources):
            return False
        if not _v1_cursors_match_records_quiet(root, v1_files):
            return False
        stale = _stale_terminal_ids_for_records(cfg, index_rows, sources)
        if stale:
            return False
    live_cids, missing = _live_refresh_targets(cfg, index_rows)
    return not live_cids and not missing


def _global_cursor_unchanged(stored: dict, fresh: dict) -> bool:
    if stored.get("prices") != fresh.get("prices"):
        return False
    if stored.get("profile_layers") != fresh.get("profile_layers"):
        return False
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


def _sync_globals(cfg, root: Path, now, *, _hold_guard: bool = False) -> set[str]:
    """Apply pending global log changes; return campaigns recomputed."""
    H = _lazy_history()
    now = H._aware(now or dt.datetime.now().astimezone())
    sources = _read_json(root / "sources.json")
    if not _sources_ok(sources):
        return set()
    if _cache_refresh_quiet(cfg, root, sources):
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
    dec_tail = (
        old_dec and new_dec
        and old_dec.get("inode") == new_dec.get("inode")
        and int(new_dec.get("size", 0)) > int(old_dec.get("size", 0))
    )
    dec_same_size_mtime = (
        old_dec and new_dec
        and old_dec.get("inode") == new_dec.get("inode")
        and old_dec.get("size") == new_dec.get("size")
        and old_dec.get("mtime_ns") != new_dec.get("mtime_ns")
    )
    if dec_tail:
        start = int(old_dec.get("offset", 0))
        dec_gaps: list[str] = []
        line_start = int(old_dec.get("lines", 0))
        new_decs, end_line, end_offset = _decision_records(
            H, cfg, cids, dec_path, dec_gaps, start=start, line_start=line_start,
        )
        tail_moved = end_line > line_start or bool(dec_gaps)
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
        if tail_moved:
            entry = (fresh.get("logs") or {}).get("conductor/decisions.jsonl")
            if entry is not None:
                entry["offset"] = end_offset
                entry["lines"] = end_line
                records_dirty = True
        if dec_gaps:
            records_dirty = True
            gaps.extend(dec_gaps)
    elif _meta_changed(old_dec, new_dec) or dec_same_size_mtime:
        old_dec_recs = [r for r in records if r.get("source") == "decisions"]
        old_ids = {r["id"] for r in old_dec_recs}
        old_by_id = {r["id"]: r for r in old_dec_recs}
        dec_gaps: list[str] = []
        new_decs, _end_line, end_offset = _decision_records(H, cfg, cids, dec_path, dec_gaps)
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
        dec_entry = (fresh.get("logs") or {}).get("conductor/decisions.jsonl")
        if dec_entry is not None:
            dec_entry["offset"] = end_offset
            dec_entry["lines"] = _end_line

    run_path = agentic / "runner.jsonl"
    old_run = old_logs.get("runner.jsonl")
    new_run = new_logs.get("runner.jsonl")
    run_same_size_mtime = (
        old_run and new_run
        and old_run.get("inode") == new_run.get("inode")
        and old_run.get("size") == new_run.get("size")
        and old_run.get("mtime_ns") != new_run.get("mtime_ns")
    )
    run_shrink = (
        old_run and new_run
        and int(new_run.get("size", 0)) < int(old_run.get("size", 0))
    )
    if _meta_changed(old_run, new_run) or run_same_size_mtime:
        runner_changed = True
        gaps_run: list[str] = []
        all_iv = H._runner_intervals(cfg, gaps_run)
        runner_down = all_iv
        tail = (
            new_run and old_run
            and new_run.get("inode") == old_run.get("inode")
            and not run_same_size_mtime
            and not run_shrink
            and new_run.get("size", 0) > old_run.get("size", 0)
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
    prices_changed = sources.get("prices") != fresh.get("prices")
    profile_changed = sources.get("profile_layers") != fresh.get("profile_layers")
    if prices_changed or profile_changed:
        for row in index_rows:
            if row.get("outcome") in _TERMINAL_OUTCOMES:
                affected.add(row["campaign"])

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

    sessions_turns_dirty = prices_changed or profile_changed
    if sessions_changed:
        _load_turns(cfg, root, sources, snap=fresh, sources_out=fresh)
        sessions_turns_dirty = True
        for row in index_rows:
            if row.get("outcome") in _TERMINAL_OUTCOMES:
                affected.add(row["campaign"])

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
            global_recs = H._global_records(cfg, cids, gaps)
        if runner_down is None:
            runner_down = H._runner_intervals(cfg, [])
        windows = {cid: _live_window_for_campaign(cfg, cid) for cid in affected if cid in live_ids}
        split_rows = _patch_index_windows(index_rows, windows)
        shares = _split_conductor_shares(cfg, root, split_rows, sources)
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
            _load_turns(cfg, root, sources, snap=fresh, sources_out=fresh)
        for cid in affected:
            if cid in live_ids:
                _set_campaign_cursors(fresh, cfg, cid)
        fresh["global_gaps"] = sorted(set(gaps))
        _persist_index_records(
            cfg,
            root,
            records_at_start=records_at_start,
            records=records,
            index_at_start=index_at_start,
            index_rows=index_rows,
            sources=fresh,
            _hold_guard=_hold_guard,
        )
    elif sessions_turns_dirty:
        _load_turns(cfg, root, sources, snap=fresh, sources_out=fresh)
        fresh.setdefault("campaign_files", dict(sources.get("campaign_files") or {}))
        fresh["global_gaps"] = sorted(set(gaps))
        fresh["complete"] = True
        _stamp_v1_files(fresh, root)
        _atomic_write_json(root / "sources.json", fresh)
    return affected


def _refresh_live(cfg, root: Path, now, index_rows: list[dict], records: list[dict],
                  *, _hold_guard: bool = False) -> tuple:
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
    if missing:
        records = _merge_records(
            records, drop_predicate=lambda r: r.get("source") not in _LOCAL_SOURCES,
        )
        records.extend(H._enrich_records(global_recs, _headers_for_enrich(index_rows)))
    touch = live_cids + missing
    windows = {cid: _live_window_for_campaign(cfg, cid) for cid in touch}
    split_rows = _patch_index_windows(index_rows, windows)
    shares = _split_conductor_shares(cfg, root, split_rows, sources)
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
        sources_out = sources or {}
        for cid in touch:
            _set_campaign_cursors(sources_out, cfg, cid)
        _persist_index_records(
            cfg,
            root,
            records_at_start=records_at_start,
            records=records,
            index_at_start=index_at_start,
            index_rows=index_rows,
            sources=sources_out,
            _hold_guard=_hold_guard,
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
    try:
        return _campaign_history_cached(cfg, cid, now, root)
    except OSError:
        note_cache_write_fallback()
        return _uncached_campaign(cfg, cid, now, cache, gap=True)


def _campaign_history_cached(cfg, cid, now, root: Path) -> dict:
    H = _lazy_history()
    _ensure_writable_cache(cfg, root, now=now)
    writes_pending = (root / "index.writes.jsonl").is_file()
    sources = _read_json(root / "sources.json") or {}
    index_rows = _read_index(root)
    index_ids = {r.get("campaign") for r in index_rows if r.get("campaign")}
    live = _live_campaign_ids(cfg)
    row = next((r for r in index_rows if r.get("campaign") == cid), None)
    if (
        not writes_pending
        and index_ids == live
        and row
        and row.get("outcome") in _TERMINAL_OUTCOMES
    ):
        hit = _doc_hit(cfg, root, cid, now, sources)
        base = store.campaign_dir(cfg, cid)
        sig = _campaign_signature_from_root_scan(base) if base is not None else None
        if (
            hit is not None
            and sig is not None
            and hit.get("signature") == sig
            and _sources_unchanged_light(cfg, sources)
        ):
            return hit

    with _cache_write_guard(root):
        _apply_pending_index_writes(cfg, root, _hold_guard=True)
        _sync_globals(cfg, root, now, _hold_guard=True)
        sources = _read_json(root / "sources.json") or {}
        index_rows = _read_index(root)
        records = _read_records(root)
        index_rows, records = _prune_gone_campaigns(
            cfg, root, index_rows, records, persist=True, _hold_guard=True,
        )
        index_ids = {r.get("campaign") for r in index_rows if r.get("campaign")}
        live = _live_campaign_ids(cfg)
        row = next((r for r in index_rows if r.get("campaign") == cid), None)
        if (
            index_ids == live
            and row
            and row.get("outcome") in _TERMINAL_OUTCOMES
        ):
            hit = _doc_hit(cfg, root, cid, now, sources)
            sig = H.campaign_signature(cfg, cid)
            if (
                hit is not None
                and hit.get("signature") == sig
                and _sources_unchanged_light(cfg, sources)
            ):
                return hit
        gaps = []
        global_recs = H._global_records(cfg, _campaign_ids(cfg), gaps)
        runner_down = H._runner_intervals(cfg, gaps)
        index_rows = _read_index(root)
        window = _live_window_for_campaign(cfg, cid)
        index_rows = _patch_one_window(index_rows, cid, window)
        shares = _split_conductor_shares(cfg, root, index_rows, sources)
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
        sources_out = _read_json(root / "sources.json") or {}
        _set_campaign_cursors(sources_out, cfg, cid)
        _persist_index_records(
            cfg,
            root,
            records_at_start=records_start,
            records=records,
            index_at_start=index_at_start,
            index_rows=index_rows,
            sources=sources_out,
            _hold_guard=True,
        )
        return doc


def history_index(cfg, *, now=None, progress=None, cache=None) -> list[dict]:
    H = _lazy_history()
    now = H._aware(now or dt.datetime.now().astimezone())
    root = _writable_root(cfg, cache)
    if root is None:
        return _uncached_index(cfg, now, progress)
    try:
        return _history_index_cached(cfg, now, progress, root)
    except OSError:
        note_cache_write_fallback()
        return _uncached_index(cfg, now, progress)


def _history_index_cached(cfg, now, progress, root: Path) -> list[dict]:
    H = _lazy_history()
    cold_filled = _ensure_writable_cache(cfg, root, now=now, progress=progress)
    if cold_filled:
        index_rows = _read_index(root)
        records = _read_records(root)
        index_rows, records = _prune_gone_campaigns(cfg, root, index_rows, records, persist=True)
        sources = _read_json(root / "sources.json") or {}
        stale = _stale_terminal_ids(cfg, index_rows, sources)
        if stale:
            records_at_start = list(_read_records(root))
            index_at_start = list(_read_index(root))
            index_rows, records, sources = _recompute_terminal_documents(
                cfg, root, now, index_rows, records, stale,
            )
            _persist_index_records(
                cfg, root,
                records_at_start=records_at_start,
                records=records,
                index_at_start=index_at_start,
                index_rows=index_rows,
                sources=sources,
            )
            index_rows = _read_index(root)
        live_cids, missing = _live_refresh_targets(cfg, index_rows)
        if live_cids or missing:
            records = _read_records(root)
            index_rows, _ = _refresh_live(cfg, root, now, index_rows, records)
            index_rows = _read_index(root)
        sources = _read_json(root / "sources.json") or {}
        shares = _conductor_shares(cfg, root, index_rows, sources)
        if shares is not None:
            _apply_conductor(index_rows, shares)
        return [_public_index_row(row) for row in _sort_index(index_rows)]

    sources = _read_json(root / "sources.json") or {}
    if _cache_refresh_quiet(cfg, root, sources):
        index_rows = _read_index(root)
        if progress:
            cids = _campaign_ids(cfg)
            for done, cid in enumerate(cids, 1):
                progress(done, len(cids))
        return [_public_index_row(row) for row in _sort_index(index_rows)]

    with _cache_write_guard(root):
        _apply_pending_index_writes(cfg, root, _hold_guard=True)
        _sync_globals(cfg, root, now, _hold_guard=True)
        index_rows = _read_index(root)
        live = _live_campaign_ids(cfg)
        index_ids = {r.get("campaign") for r in index_rows if r.get("campaign")}
        need_prune = bool(index_ids - live)
        sources = _read_json(root / "sources.json") or {}
        records = None
        if need_prune:
            records = _read_records(root)
            index_rows, records = _prune_gone_campaigns(
                cfg, root, index_rows, records, persist=True, _hold_guard=True,
            )
        stale = _stale_terminal_ids(cfg, index_rows, sources)
        if stale:
            if records is None:
                records = _read_records(root)
            records_at_start = list(_read_records(root))
            index_at_start = list(_read_index(root))
            index_rows, records, sources = _recompute_terminal_documents(
                cfg, root, now, index_rows, records, stale,
            )
            _persist_index_records(
                cfg, root,
                records_at_start=records_at_start,
                records=records,
                index_at_start=index_at_start,
                index_rows=index_rows,
                sources=sources,
                _hold_guard=True,
            )
            index_rows = _read_index(root)
        live_cids, missing = _live_refresh_targets(cfg, index_rows)
        if live_cids or missing:
            if records is None:
                records = _read_records(root)
            index_rows, _ = _refresh_live(
                cfg, root, now, index_rows, records, _hold_guard=True,
            )
            index_rows = _read_index(root)
        shares = _conductor_shares(cfg, root, index_rows, sources)
        if shares is not None:
            _apply_conductor(index_rows, shares)
        if progress:
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
    try:
        return _history_records_cached(
            cfg, now=now, root=root, campaign=campaign, agent=agent, kind=kind,
            project=project, severity=severity, status=status, since=since, until=until,
            query=query, limit=limit,
        )
    except OSError:
        note_cache_write_fallback()
        return _uncached_records(
            cfg, campaign=campaign, agent=agent, kind=kind, project=project,
            severity=severity, status=status, since=since, until=until, query=query, limit=limit,
        )


def _history_records_cached(cfg, *, now, root: Path, campaign=None, agent=None, kind=None,
                            project=None, severity=None, status=None, since=None, until=None,
                            query=None, limit=None) -> list[dict]:
    H = _lazy_history()
    _ensure_writable_cache(cfg, root, now=now)
    sources = _read_json(root / "sources.json") or {}
    kind_set = H._set(kind)
    needle = str(query or "").casefold()
    extra = any(x is not None for x in (campaign, agent, project, severity, status, since, until))
    stream_kinds = kind_set if kind_set and not extra else None
    stream_needle = needle if needle and not extra else ""
    if _records_cache_quiet(cfg, root, sources):
        if stream_kinds is not None or stream_needle:
            base = _read_records_filtered(root, kinds=stream_kinds, needle=stream_needle)
        else:
            base = _read_records(root)
        values = {name: H._set(value) for name, value in (
            ("campaign", campaign), ("agent", agent), ("kind", kind), ("project", project),
            ("severity", severity), ("status", status),
        )}
        since_b, until_b = H._date_bound(since), H._date_bound(until, until=True)
        out = []
        for rec in base:
            if any(wanted is not None and rec.get(name) not in wanted for name, wanted in values.items()):
                continue
            if not H._in_dates(rec.get("ts"), since_b, until_b):
                continue
            if needle and stream_needle != needle and needle not in _search_text(rec):
                continue
            out.append(rec)
        need_sort = (
            extra
            or since is not None
            or until is not None
            or (needle and stream_needle != needle)
        )
        if need_sort:
            out.sort(key=_records_sort_key, reverse=True)
        return out[:limit] if limit is not None else out
    with _cache_write_guard(root):
        if (root / "index.writes.jsonl").is_file():
            _apply_pending_index_writes(cfg, root, _hold_guard=True)
            sources = _read_json(root / "sources.json") or {}
        _sync_globals(cfg, root, now, _hold_guard=True)
        index_rows = _read_index(root)
        live = _live_campaign_ids(cfg)
        index_ids = {r.get("campaign") for r in index_rows if r.get("campaign")}
        need_prune = bool(index_ids - live)
        sources = _read_json(root / "sources.json") or {}
        records: list[dict] | None = None
        if need_prune:
            records = _read_records(root)
            index_rows, records = _prune_gone_campaigns(
                cfg, root, index_rows, records, persist=True, _hold_guard=True,
            )
        stale = _stale_terminal_ids_for_records(cfg, index_rows, sources)
        if stale:
            if records is None:
                records = _read_records(root)
            records_at_start = list(records)
            index_at_start = list(index_rows)
            index_rows, records, sources = _recompute_terminal_documents(
                cfg, root, now, index_rows, records, stale,
            )
            _persist_index_records(
                cfg, root,
                records_at_start=records_at_start,
                records=records,
                index_at_start=index_at_start,
                index_rows=index_rows,
                sources=sources,
                _hold_guard=True,
            )
        live_cids, missing = _live_refresh_targets(cfg, index_rows)
        if live_cids or missing:
            if records is None:
                records = _read_records(root)
            index_rows, records = _refresh_live(
                cfg, root, now, index_rows, records, _hold_guard=True,
            )
    kind_set = H._set(kind)
    needle = str(query or "").casefold()
    extra = any(x is not None for x in (campaign, agent, project, severity, status, since, until))
    stream_kinds = kind_set if kind_set and not extra else None
    stream_needle = needle if needle and not extra else ""
    if records is not None:
        base = records
    elif stream_kinds is not None or stream_needle:
        mem = _get_records_mem(root)
        if mem is not None:
            entries = mem["entries"]
            if stream_kinds is not None and not stream_needle and len(stream_kinds) == 1:
                idxs = mem.get("by_kind", {}).get(next(iter(stream_kinds)))
                if idxs is not None:
                    base = [entries[i][0] for i in idxs]
                else:
                    base = []
            else:
                base = []
                for rec, search in entries:
                    if stream_kinds is not None and rec.get("kind") not in stream_kinds:
                        continue
                    if stream_needle and stream_needle not in search:
                        continue
                    base.append(rec)
        else:
            base = _read_records_filtered(root, kinds=stream_kinds, needle=stream_needle)
    else:
        base = _read_records(root)
    values = {name: H._set(value) for name, value in (
        ("campaign", campaign), ("agent", agent), ("kind", kind), ("project", project),
        ("severity", severity), ("status", status),
    )}
    since_b, until_b = H._date_bound(since), H._date_bound(until, until=True)
    out = []
    for rec in base:
        if any(wanted is not None and rec.get(name) not in wanted for name, wanted in values.items()):
            continue
        if not H._in_dates(rec.get("ts"), since_b, until_b):
            continue
        if needle and stream_needle != needle and needle not in _search_text(rec):
            continue
        out.append(rec)
    out.sort(key=_records_sort_key, reverse=True)
    return out[:limit] if limit is not None else out


def rebuild_cache(cfg, *, now=None, progress=None) -> dict:
    root = _writable_root(cfg, True)
    if root is None:
        raise OSError("cache: not writable")
    with _cache_write_guard(root):
        _clear_v1_except_lock(root)
        return _cold_fill(cfg, root, now=now, progress=progress, _hold_guard=True)
