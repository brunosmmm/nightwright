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
    return path(cfg).with_suffix(".lock")


def validate_url(url: str) -> str:
    u = urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError(f"url {url!r} must be http(s)://HOST")
    return url.rstrip("/")


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
    text = json.dumps({"schema": SCHEMA, "hosts": recs}, indent=2) + "\n"
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, p)


def update(cfg, fn) -> list[dict]:
    """Exclusive flock, read, `fn(records)`, fsync write; return the new list."""
    p, lk = path(cfg), _lock(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(lk, "a+b") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        recs = read_strict(cfg)
        fn(recs)
        _write_fsync(p, recs)
        return recs


def find(records: list[dict], name: str) -> dict:
    for r in records:
        if r.get("name") == name:
            return r
    raise ValueError(f"no host named {name!r}")
