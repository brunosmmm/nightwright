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


@contextmanager
def _config_lock(cdir: Path):
    fd = os.open(cdir / "config.yaml.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _ensure_config_dir_for_set(cdir: Path) -> str | None:
    if cdir.exists():
        if not stat.S_ISDIR(cdir.lstat().st_mode):
            return "config directory is not a directory"
        return None
    to_create = []
    p = cdir
    while not p.exists():
        to_create.append(p)
        p = p.parent
    for d in reversed(to_create):
        d.mkdir(mode=0o700)
        os.chmod(d, 0o700)
    if (cdir.stat().st_mode & 0o777) != 0o700:
        os.chmod(cdir, 0o700)
    return None


def _load_mapping(path: Path) -> tuple[dict | None, str | None]:
    if not path.is_file():
        return {}, None
    st = path.lstat()
    if stat.S_ISLNK(st.st_mode):
        return None, "config.yaml is a symlink"
    if not stat.S_ISREG(st.st_mode):
        return None, "config.yaml is not a regular file"
    raw = path.read_bytes()
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
    if not cdir.is_dir():
        return {}, None
    path = config_path()
    doc, err = _load_mapping(path)
    if err:
        return {}, _result(ok=False, error=err)
    if doc is None:
        return {}, _result(ok=False, error="config.yaml is not a mapping")
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


def set_key(cfg: dict, key: str, value: str) -> dict:
    validate_value(key, value)
    cdir = config_dir()
    dir_err = _ensure_config_dir_for_set(cdir)
    if dir_err:
        return _result(ok=False, error=dir_err)
    path = config_path()
    with _config_lock(cdir):
        if path.exists():
            st = path.lstat()
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                return _result(ok=False, error="config.yaml is not a regular file")
        doc, err = _load_mapping(path) if path.is_file() else ({}, None)
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
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            if path.is_file():
                stamp = datetime.now().strftime("%Y%m%d%H%M%S")
                bak = path.with_name(f"config.yaml.bak.{stamp}")
                while bak.exists():
                    bak = path.with_name(f"config.yaml.bak.{stamp}-{os.getpid()}")
                bak.write_bytes(path.read_bytes())
                os.chmod(bak, 0o600)
            tmp.write_text(text, encoding="utf-8")
            os.chmod(tmp, 0o600)
            tmp_fd = os.open(tmp, os.O_RDONLY)
            try:
                os.fsync(tmp_fd)
            finally:
                os.close(tmp_fd)
            os.replace(tmp, path)
            os.chmod(path, 0o600)
        except OSError as e:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
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
