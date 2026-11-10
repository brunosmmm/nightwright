"""SPEC-0538: allowlisted read/write of agentic.workers.executor on the worker host."""
from __future__ import annotations

import fcntl
import json
import os
import stat
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import yaml

from .. import paths
from . import executors as EX

SCHEMA = "nw.worker.config_set.result.v1"
EXIT_REFUSE = 2
EXIT_FILE = 3

ALLOWED: dict[str, frozenset[str]] = {
    "executor": frozenset({"local", "slurm"}),
}

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def validate_key(key: str) -> None:
    if key not in ALLOWED:
        raise ValueError(f"key {key!r} is not allowed")


def validate_value(key: str, value: str) -> None:
    validate_key(key)
    if value not in ALLOWED[key]:
        raise ValueError(f"value {value!r} is not allowed for {key}")


def config_dir() -> Path:
    return Path(paths.config_dir())


def config_path() -> Path:
    return config_dir() / "config.yaml"


def _result(**kw) -> dict:
    return {"schema": SCHEMA, **kw}


def _emit(rec: dict) -> None:
    print(json.dumps(rec), flush=True)


def _effective(cfg: dict) -> str:
    return EX.configured(cfg)


def _merge_file(cfg: dict, doc: dict) -> dict:
    merged = dict(cfg)
    agentic = dict(merged.get("agentic") or {})
    file_ag = doc.get("agentic")
    if isinstance(file_ag, dict):
        fw = file_ag.get("workers")
        wk = dict(agentic.get("workers") or {}) if isinstance(agentic.get("workers"), dict) else {}
        if isinstance(fw, dict):
            wk = {**wk, **fw}
        agentic = {**agentic, **{k: v for k, v in file_ag.items() if k != "workers"}}
        agentic["workers"] = wk
    merged["agentic"] = agentic
    return merged


def _nested_get(doc: dict, key: str):
    if key != "executor":
        raise ValueError(key)
    node = doc.get("agentic")
    if not isinstance(node, dict):
        return None
    node = node.get("workers")
    if not isinstance(node, dict):
        return None
    return node.get("executor")


def _path_lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


@contextmanager
def _config_lock(cdir: Path):
    lock = cdir / "config.yaml.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _dir_path_error(st: os.stat_result, label: str) -> str | None:
    if stat.S_ISLNK(st.st_mode):
        return f"{label} is not a directory"
    if not stat.S_ISDIR(st.st_mode):
        return f"{label} is not a directory"
    return None


def _write_fd(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        if n <= 0:
            raise OSError("short write")
        view = view[n:]


def _ensure_config_dir_for_set(cdir: Path) -> str | None:
    st = _path_lstat(cdir)
    if st is not None:
        if stat.S_ISLNK(st.st_mode):
            return "config directory is not a directory"
        return _dir_path_error(st, "config directory")
    to_create: list[Path] = []
    p = cdir
    while True:
        pst = _path_lstat(p)
        if pst is not None:
            if stat.S_ISLNK(pst.st_mode):
                try:
                    pst = p.stat()
                except OSError as e:
                    return f"config directory parent unusable: {e}"
                if not stat.S_ISDIR(pst.st_mode):
                    return "config directory is not a directory"
                break
            if err := _dir_path_error(pst, "config directory"):
                return err
            break
        to_create.append(p)
        if p.parent == p:
            break
        p = p.parent
    for d in reversed(to_create):
        d.mkdir(mode=0o700)
        os.chmod(d, 0o700)
    st = cdir.stat()
    if (st.st_mode & 0o777) != 0o700:
        os.chmod(cdir, 0o700)
    return None


def _read_config_bytes(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW)
    try:
        chunks: list[bytes] = []
        while True:
            block = os.read(fd, 1 << 20)
            if not block:
                break
            chunks.append(block)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _load_mapping(path: Path) -> tuple[dict | None, str | None]:
    st = _path_lstat(path)
    if st is None:
        return {}, None
    if stat.S_ISLNK(st.st_mode):
        return None, "config.yaml is a symlink"
    if not stat.S_ISREG(st.st_mode):
        return None, "config.yaml is not a regular file"
    try:
        raw = _read_config_bytes(path)
    except OSError as e:
        return None, f"config.yaml unreadable: {e}"
    if not raw.strip():
        return {}, None
    try:
        doc = yaml.safe_load(raw.decode("utf-8"))
    except yaml.YAMLError:
        return None, "config.yaml is not valid YAML"
    if doc is None:
        return {}, None
    if not isinstance(doc, dict):
        return None, "config.yaml is not a mapping"
    return doc, None


def _mapping_nodes_ok(doc: dict) -> str | None:
    ag = doc.get("agentic")
    if ag is not None and not isinstance(ag, dict):
        return "agentic is not a mapping"
    if isinstance(ag, dict):
        wk = ag.get("workers")
        if wk is not None and not isinstance(wk, dict):
            return "agentic.workers is not a mapping"
    return None


def _stored_doc() -> tuple[dict, dict | None]:
    """(stored mapping for reads, file-rule error dict or None)."""
    cdir = config_dir()
    st = _path_lstat(cdir)
    if st is None:
        return {}, None
    if _dir_path_error(st, "config directory"):
        return {}, _result(ok=False, error="config directory is not a directory")
    path = config_path()
    doc, err = _load_mapping(path)
    if err:
        return {}, _result(ok=False, error=err)
    if doc is None:
        return {}, _result(ok=False, error="config.yaml is not a mapping")
    if bad := _mapping_nodes_ok(doc):
        return {}, _result(ok=False, error=bad)
    return doc, None


def get_key(cfg: dict, key: str | None) -> dict:
    if key is None or key == "":
        stored, err = _stored_doc()
        if err:
            return err
        keys = {}
        eff = _effective(_merge_file(cfg, stored))
        for k in ALLOWED:
            raw = _nested_get(stored, k)
            keys[k] = {"value": raw if isinstance(raw, str) else None, "effective": eff}
        return _result(ok=True, op="get", keys=keys)
    validate_key(key)
    stored, err = _stored_doc()
    if err:
        return err
    raw = _nested_get(stored, key)
    value = raw if isinstance(raw, str) else None
    eff = _effective(_merge_file(cfg, stored))
    return _result(ok=True, op="get", key=key, value=value, effective=eff)


def _set_nested(doc: dict, key: str, value: str) -> dict:
    new = dict(doc)
    ag = dict(new.get("agentic") or {})
    wk = dict(ag.get("workers") or {}) if isinstance(ag.get("workers"), dict) else {}
    wk["executor"] = value
    ag["workers"] = wk
    new["agentic"] = ag
    return new


def _backup_path(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    base = path.with_name(f"config.yaml.bak.{stamp}")
    if not base.exists():
        return base
    pid = os.getpid()
    alt = path.with_name(f"config.yaml.bak.{stamp}-{pid}")
    if not alt.exists():
        return alt
    n = 0
    while True:
        n += 1
        cand = path.with_name(f"config.yaml.bak.{stamp}-{pid}-{n}")
        if not cand.exists():
            return cand


def _exclusive_temp(cdir: Path) -> tuple[int, Path]:
    pid = os.getpid()
    for n in range(32):
        name = f"config.yaml.tmp.{pid}" if n == 0 else f"config.yaml.tmp.{pid}.{n}"
        tmp = cdir / name
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW, 0o600)
            return fd, tmp
        except FileExistsError:
            continue
    raise OSError("could not create config temp file")


def set_key(cfg: dict, key: str, value: str) -> dict:
    validate_value(key, value)
    cdir = config_dir()
    dir_err = _ensure_config_dir_for_set(cdir)
    if dir_err:
        return _result(ok=False, error=dir_err)
    path = config_path()
    with _config_lock(cdir):
        path_st = _path_lstat(path)
        if path_st is not None:
            if stat.S_ISLNK(path_st.st_mode) or not stat.S_ISREG(path_st.st_mode):
                return _result(ok=False, error="config.yaml is not a regular file")
        doc, err = _load_mapping(path) if path_st is not None else ({}, None)
        if err:
            return _result(ok=False, error=err)
        assert doc is not None
        bad = _mapping_nodes_ok(doc)
        if bad:
            return _result(ok=False, error=bad)
        cur = _nested_get(doc, key)
        unchanged = isinstance(cur, str) and cur == value
        if unchanged:
            eff = _effective(_merge_file(cfg, doc))
            return _result(ok=True, op="set", key=key, value=value, effective=eff, unchanged=True)
        new_doc = _set_nested(doc, key, value)
        text = yaml.safe_dump(new_doc, sort_keys=False, default_flow_style=False)
        bak: Path | None = None
        bak_created = False
        tmp: Path | None = None
        tmp_fd: int | None = None
        replaced = False
        try:
            if path_st is not None:
                prev_bytes = _read_config_bytes(path)
                bak = _backup_path(path)
                bfd = os.open(bak, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW, 0o600)
                bak_created = True
                try:
                    _write_fd(bfd, prev_bytes)
                    os.fsync(bfd)
                finally:
                    os.close(bfd)
            tmp_fd, tmp = _exclusive_temp(cdir)
            _write_fd(tmp_fd, text.encode("utf-8"))
            os.fsync(tmp_fd)
            os.fchmod(tmp_fd, 0o600)
            os.close(tmp_fd)
            tmp_fd = None
            os.replace(tmp, path)
            tmp = None
            replaced = True
        except OSError as e:
            if tmp_fd is not None:
                try:
                    os.close(tmp_fd)
                except OSError:
                    pass
            if tmp is not None and not replaced:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
            if bak is not None and bak_created and not replaced:
                try:
                    bak.unlink(missing_ok=True)
                except OSError:
                    pass
            return _result(ok=False, error=str(e))
        eff = _effective(_merge_file(cfg, new_doc))
        return _result(ok=True, op="set", key=key, value=value, effective=eff, unchanged=False)


def run(argv: list[str], cfg: dict) -> int:
    if not argv:
        _emit(_result(ok=False, error="missing operation"))
        return EXIT_REFUSE
    op = argv[0]
    if op == "get":
        if len(argv) > 2:
            _emit(_result(ok=False, error="too many arguments for get"))
            return EXIT_REFUSE
        key = argv[1] if len(argv) == 2 else None
        if key is not None:
            try:
                validate_key(key)
            except ValueError as e:
                _emit(_result(ok=False, error=str(e)))
                return EXIT_REFUSE
        rec = get_key(cfg, key)
        if not rec.get("ok"):
            _emit(rec)
            return EXIT_FILE
        _emit(rec)
        return 0
    if op == "set":
        if len(argv) != 3:
            _emit(_result(ok=False, error="set requires KEY VALUE"))
            return EXIT_REFUSE
        key, value = argv[1], argv[2]
        try:
            validate_value(key, value)
        except ValueError as e:
            _emit(_result(ok=False, error=str(e)))
            return EXIT_REFUSE
        rec = set_key(cfg, key, value)
        if not rec.get("ok"):
            _emit(rec)
            return EXIT_FILE
        _emit(rec)
        return 0
    _emit(_result(ok=False, error=f"unknown operation {op!r}"))
    return EXIT_REFUSE


def parse_config_set_result(stdout: str) -> tuple[dict | None, str]:
    last = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("schema") == SCHEMA:
            last = obj
    if last is None:
        return None, "no config-set result on stdout"
    return last, ""
