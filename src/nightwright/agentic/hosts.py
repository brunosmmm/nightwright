"""Federation host registry (SPEC-0529): `<agentic_dir>/hosts.json` (`nw.hosts.v1`). Inert when
the file is missing."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from . import workers as WK
from .store import agentic_dir

SCHEMA = "nw.hosts.v1"
NAME_RE = WK.NAME_RE
HOST_KEYS = frozenset({"name", "url", "token_sha256", "secret_ref", "enabled", "status_cache"})


def path(cfg) -> Path:
    return agentic_dir(cfg) / "hosts.json"


def _lock(cfg) -> Path:
    p = path(cfg)
    return p.with_name(p.name + ".lock")


def validate_url(url: str) -> str:
    u = urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError(f"url {url!r} must be http(s)://HOST")
    if u.username or u.password or u.query or u.fragment:
        raise ValueError(f"url {url!r} must be http(s)://HOST[:PORT]")
    if u.path not in ("", "/"):
        raise ValueError(f"url {url!r} must be http(s)://HOST[:PORT]")
    if "\r" in url or "\n" in url:
        raise ValueError(f"url {url!r} must be http(s)://HOST[:PORT]")
    host = u.hostname
    if ":" in host:
        host = f"[{host}]"
    port = f":{u.port}" if u.port is not None else ""
    return f"{u.scheme}://{host}{port}"


def load(cfg) -> list[dict]:
    """Every host record, or [] when there is no registry."""
    p = path(cfg)
    if not p.is_file():
        return []
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        return []
    hosts = doc.get("hosts")
    return list(hosts) if isinstance(hosts, list) else []


def read_strict(cfg) -> list[dict]:
    """Like `load`, but raises when the file exists and does not parse."""
    p = path(cfg)
    if not p.is_file():
        return []
    doc = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise ValueError(f"{p}: not {SCHEMA}")
    hosts = doc.get("hosts")
    if not isinstance(hosts, list):
        raise ValueError(f"{p}: hosts is not a list")
    return hosts


def _write_fsync(p: Path, recs: list) -> None:
    data = (json.dumps({"schema": SCHEMA, "hosts": recs}, indent=2) + "\n").encode("utf-8")
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        off = 0
        while off < len(data):
            n = os.write(fd, data[off:])
            if n <= 0:
                raise OSError("short write")
            off += n
        os.fsync(fd)
    except OSError:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    else:
        os.close(fd)
    os.replace(tmp, p)


def _acquire_lock(cfg):
    p = path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    lk = _lock(cfg)
    fd = os.open(lk, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def update(cfg, fn) -> list[dict]:
    """Exclusive flock, read, `fn(records)`, fsync write; return the new list."""
    p = path(cfg)
    fd = _acquire_lock(cfg)
    try:
        recs = read_strict(cfg)
        fn(recs)
        _write_fsync(p, recs)
        return recs
    finally:
        os.close(fd)


def find(records: list[dict], name: str) -> dict:
    for r in records:
        if r.get("name") == name:
            return r
    raise ValueError(f"no host named {name!r}")
