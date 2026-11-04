"""Dashboard server auth (SPEC-0414): the loopback-only bind gate, the per-host bearer token and
the Host/Origin (DNS rebinding) check. Pure functions; nothing here opens a socket or runs
`tailscale` (the tailnet slice adds that). Not imported by `snapshot` or any queue-side module."""
from __future__ import annotations

import ipaddress
import os
import secrets

from . import dashboard_dir

DEFAULT_PORT = 8791


class BindRefused(ValueError):
    """A bind spec that is malformed or that the bind rule refuses (the CLI maps it to exit 2)."""


def _ip(host: str):
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def parse_bind(spec) -> tuple[str, int]:
    """`ADDR:PORT`, `[v6]:PORT`, `localhost[:PORT]` (-> `127.0.0.1`); a missing port is 8791 and
    port 0 is ephemeral. A host that is not an IP after the `localhost` mapping is returned as
    given (`check_bind` refuses it). `BindRefused` for a malformed spec or a port over 65535."""
    s = spec.strip() if isinstance(spec, str) else ""
    bad = BindRefused(f"--bind {spec!r} is not ADDR:PORT")
    port_s = None
    if s.startswith("["):
        host, close, rest = s[1:].partition("]")
        if not close or (rest and not rest.startswith(":")):
            raise bad
        port_s = rest[1:] if rest else None
    elif _ip(s) is not None:
        host = s
    elif s.count(":") > 1:
        raise bad
    else:
        host, sep, tail = s.partition(":")
        port_s = tail if sep else None
    if not host:
        raise bad
    port = DEFAULT_PORT
    if port_s is not None:
        if not (port_s.isascii() and port_s.isdigit()) or int(port_s) > 65535:
            raise BindRefused(f"--bind {spec!r}: the port must be 0-65535")
        port = int(port_s)
    if host.lower() == "localhost":
        host = "127.0.0.1"
    return host, port


def check_bind(host: str) -> str | None:
    """Why `host` may not be bound, else None. This server binds loopback only."""
    ip = _ip(host)
    if ip is None:
        return f"{host} is not an IP address"
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_unspecified:
        return f"{host} is a wildcard address"
    if ip.is_loopback:
        return None
    return f"{host} is not a loopback address (this server binds loopback only)"


def validate_bind(spec) -> tuple[str, int]:
    """`parse_bind` then `check_bind`; `BindRefused` with the rule. Both the CLI and
    `make_server` call it, so no caller can skip the gate."""
    host, port = parse_bind(spec)
    why = check_bind(host)
    if why:
        raise BindRefused(f"refusing to bind: {why}")
    return host, port


def local_token(cfg) -> str:
    """`<dashboard_dir>/local.token` (0600): read, or created on first use. A looser file is
    chmod-ed to 0600 and an empty one is regenerated."""
    p = dashboard_dir(cfg) / "local.token"
    try:
        tok = p.read_text(encoding="utf-8").strip()
    except OSError:
        tok = ""
    if tok:
        if p.stat().st_mode & 0o777 != 0o600:
            os.chmod(p, 0o600)
        return tok
    tok = secrets.token_urlsafe(32)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, (tok + "\n").encode("ascii"))
    finally:
        os.close(fd)
    return tok


def host_values(bound_host: str, port: int) -> set[str]:
    """The `Host` values accepted: the bound address with the port (an IPv6 one in brackets) and
    `localhost:port`."""
    h = f"[{bound_host}]" if ":" in bound_host else bound_host
    return {f"{h}:{port}".lower(), f"localhost:{port}"}


def check_host_origin(headers, allowed: set[str]) -> str | None:
    """The audit reason (`bad_host` / `bad_origin`) a request is refused for, else None.
    `Origin`, when present, must be `http://` plus an allowed host; `null` is refused."""
    host = headers.get("Host")
    if host is None or host.strip().lower() not in allowed:
        return "bad_host"
    origin = headers.get("Origin")
    if origin is not None:
        o = origin.strip().lower()
        if not o.startswith("http://") or o[len("http://"):] not in allowed:
            return "bad_origin"
    return None
