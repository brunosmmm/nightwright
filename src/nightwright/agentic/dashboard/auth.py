"""Dashboard server auth (SPEC-0414, SPEC-0416): the bind gate, the per-host bearer token and the
Host/Origin (DNS rebinding) check. The gate accepts loopback only; it learns this host's own
tailnet addresses (`tailscale ip`, the only `tailscale` form run here) but refuses to serve on
them until the identity gate is wired into the server. `Gate` (SPEC-0417) is that gate's decision
logic (`tailscale whois`); nothing calls it yet. Nothing here opens a socket. Not imported by `snapshot` or
any queue-side module."""
from __future__ import annotations

import contextlib
import dataclasses
import ipaddress
import json
import os
import secrets
import subprocess
import tempfile
import threading
import time

from . import dashboard_dir

DEFAULT_PORT = 8791


TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


class BindRefused(ValueError):
    """A bind spec that is malformed or that the bind rule refuses (the CLI maps it to exit 2)."""


class TailnetMissing(RuntimeError):
    """This host has no tailnet address (`tailscale` missing, logged out, stopped or failing).
    The CLI maps it to exit 3, so a supervisor can tell it from a crash (exit 1) or a refusal (2)."""


class TailscaleError(RuntimeError):
    """`run_tailscale` could not get output from `tailscale`; `own_tailnet_ips` turns it into
    `TailnetMissing`."""


def _ip(host: str):
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _unmapped(ip):
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def is_tailnet_ip(addr) -> bool:
    """`addr` (a string or an `ipaddress` object) is in `100.64.0.0/10` or `fd7a:115c:a1e0::/48`;
    an IPv4-mapped IPv6 address is unmapped first."""
    ip = _ip(addr) if isinstance(addr, str) else addr
    if ip is None:
        return False
    ip = _unmapped(ip)
    return ip in (TAILNET_V4 if ip.version == 4 else TAILNET_V6)


_NO_TAILNET = (
    "no tailnet address on this host (`tailscale` is missing, logged out or stopped): "
    "run `tailscale up`, or bind loopback with `--bind 127.0.0.1:PORT`")


def run_tailscale(args, *, timeout: float = 5.0) -> str:
    """stdout of `tailscale <args>` (an argv list, no shell). `TailscaleError` for a missing
    binary, a timeout, a non-zero exit or output that is not UTF-8."""
    try:
        r = subprocess.run(["tailscale", *args], capture_output=True, text=True,
                           timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise TailscaleError(f"`tailscale {' '.join(args)}` timed out")
    except UnicodeError:                        # a ValueError subclass: must come first
        raise TailscaleError("`tailscale` printed non-UTF-8 output")
    except (OSError, ValueError) as e:          # not on PATH, not executable
        raise TailscaleError(f"cannot run `tailscale`: {e}")
    if r.returncode != 0:
        raise TailscaleError(f"`tailscale {' '.join(args)}` exited {r.returncode}")
    return r.stdout


def own_tailnet_ips(run=run_tailscale) -> list[str]:
    """This host's tailnet addresses from `tailscale ip` (IPv4 first, de-duplicated).
    `TailnetMissing` when there are none."""
    try:
        out = run(["ip"])
    except TailscaleError:
        out = ""
    ips: list[str] = []
    for line in out.splitlines():
        line = line.strip()
        ip = _ip(line)
        if ip is not None and is_tailnet_ip(ip):
            ips.append(str(ip))
    if not ips:
        raise TailnetMissing(_NO_TAILNET)
    return sorted(dict.fromkeys(ips), key=lambda a: ":" in a)


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


def check_bind(host: str, own=()) -> str | None:
    """Why `host` may not be bound, else None. Loopback is accepted; an address in the Tailscale
    ranges is accepted only if it is in `own` (this host's own tailnet addresses); everything
    else is refused. `validate_bind` passes no `own`, so it stays loopback-only."""
    ip = _ip(host)
    if ip is None:
        return f"{host} is not an IP address"
    ip = _unmapped(ip)
    if ip.is_unspecified:
        return f"{host} is a wildcard address"
    if ip.is_loopback:
        return None
    if is_tailnet_ip(ip):
        if any(_ip(o) == ip for o in own):
            return None
        return (f"{host} is a foreign tailnet address (not one of this host's own, and not a "
                f"loopback address)")
    return f"{host} is not a loopback or tailnet address (this server binds loopback only)"


def validate_bind(spec) -> tuple[str, int]:
    """Loopback-only `parse_bind` then `check_bind` (SPEC-0414); `BindRefused` with the rule.
    The dashboard itself goes through `resolve_bind`."""
    host, port = parse_bind(spec)
    why = check_bind(host)
    if why:
        raise BindRefused(f"refusing to bind: {why}")
    return host, port


def resolve_bind(spec, own_fn=own_tailnet_ips) -> tuple[str, int]:
    """The one bind entry point (SPEC-0416): parse, resolve `tailscale[:PORT]` to this host's
    first own tailnet address, run `check_bind`. `own_fn` is called once for `tailscale` and for
    an address in the Tailscale ranges, never otherwise; `TailnetMissing` when it fails.
    `BindRefused` for everything refused, including (for now) an accepted own tailnet address."""
    host, port = parse_bind(spec)
    own: list[str] = []
    if host.lower() == "tailscale":
        own = own_fn()
        if not own:
            raise TailnetMissing(_NO_TAILNET)
        host = own[0]
    elif is_tailnet_ip(host):
        own = own_fn()
    why = check_bind(host, own)
    if why:
        raise BindRefused(f"refusing to bind: {why}")
    if not _unmapped(_ip(host)).is_loopback:
        # The sibling slice (tailnet whois identity gate) removes exactly this refusal.
        raise BindRefused(
            f"refusing to bind {host}: tailnet serving needs the identity gate, which is not "
            f"available yet; bind loopback with `--bind 127.0.0.1:PORT`")
    return host, port


def _read_token(p) -> str:
    try:
        return p.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):               # missing, unreadable or not UTF-8: regenerate
        return ""


def local_token(cfg) -> str:
    """`<dashboard_dir>/local.token` (0600): read, or created on first use. A looser file is
    chmod-ed to 0600; an empty or unreadable (not UTF-8) one is regenerated. Creation is
    exclusive (the token is written to a private temp file, then hard-linked into place), so two
    servers starting together on one data dir end up with the same token."""
    d = dashboard_dir(cfg)
    p = d / "local.token"
    tok = _read_token(p)
    if not tok:
        tok = secrets.token_urlsafe(32)
        fd, tmp = tempfile.mkstemp(prefix=".local.token.", dir=d)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, (tok + "\n").encode("ascii"))
            os.close(fd)
            try:
                os.link(tmp, p)                 # fails if another process won the race
            except FileExistsError:
                other = _read_token(p)
                if other:
                    return other
                os.replace(tmp, p)              # an empty or corrupt file: take it over
                return tok
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        return tok
    if p.stat().st_mode & 0o777 != 0o600:
        os.chmod(p, 0o600)
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


def bind_mode(host: str) -> str:
    """`"token"` for a loopback bind host, `"whois"` for every other host string (SPEC-0417)."""
    ip = _ip(host) if isinstance(host, str) else None
    return "token" if ip is not None and _unmapped(ip).is_loopback else "whois"


def _peer_ip(addr) -> str | None:
    """A peer address as a normalised IP string: IPv6 zone (`%...`) stripped, IPv4-mapped IPv6
    unmapped, re-rendered by `ipaddress`; None when it is not an IP."""
    if not isinstance(addr, str):
        return None
    ip = _ip(addr.strip().partition("%")[0])
    return None if ip is None else str(_unmapped(ip))


@dataclasses.dataclass(frozen=True)
class Verdict:
    """The gate's answer for one peer. `fresh` is true when this call ran `tailscale`, false when
    it came from the cache. `login` / `node` / `node_ip` are set when whois said who it is."""
    allowed: bool
    reason: str
    login: str | None = None
    node: str | None = None
    node_ip: str | None = None
    fresh: bool = False


ALLOWED_TTL_S = 60.0
DENIED_TTL_S = 5.0
CACHE_MAX = 512


def _norm_login(v) -> str:
    return v.strip().casefold() if isinstance(v, str) else ""


def _dns_name(v) -> str | None:
    n = v.strip().rstrip(".").lower() if isinstance(v, str) else ""
    return n or None


class Gate:
    """The identity gate's decision point (SPEC-0417): `check(peer_ip)` asks
    `tailscale whois --json <peer-ip>` who a tailnet peer is and returns a `Verdict`. Fails
    closed; never raises. `allowed_logins` empty means this host's own login only (from
    `tailscale status --json`). Verdicts are cached per peer (60 s allowed, 5 s denied). The only
    `tailscale` forms run are `status --json` and `whois --json <ip>`. Reads no config or
    environment."""

    def __init__(self, run=run_tailscale, allowed_logins=(), clock=time.monotonic):
        self._run = run
        self._clock = clock
        if isinstance(allowed_logins, str):
            allowed_logins = [allowed_logins]
        self._explicit = frozenset(n for n in map(_norm_login, allowed_logins or ()) if n)
        self._lock = threading.Lock()           # guards _cache, _locks; never held over `run`
        self._cache: dict[str, tuple[float, Verdict]] = {}
        self._locks: dict[str, list] = {}       # per-peer [lock, waiters]: single-flight misses
        self._self_lock = threading.Lock()
        self._self: tuple[float, str | None, str | None] | None = None

    # --- the host's own login / MagicDNS name (one memoised `status --json` read) -------------
    def _self_info(self) -> tuple[str | None, str | None]:
        with self._self_lock:
            now = self._clock()
            if self._self is None or now >= self._self[0]:
                login, dns = self._read_status()
                ttl = ALLOWED_TTL_S if login is not None else DENIED_TTL_S
                self._self = (now + ttl, login, dns)
            return self._self[1], self._self[2]

    def _read_status(self) -> tuple[str | None, str | None]:
        try:
            d = json.loads(self._run(["status", "--json"]))
            me = d["Self"]
            dns = _dns_name(me.get("DNSName"))
            if me.get("Tags"):                  # a tagged host has no login
                return None, dns
            uid = me.get("UserID")
            login = _norm_login(d["User"][str(uid)]["LoginName"]) if uid else ""
            return (login or None), dns
        except Exception:                       # failed, garbage, no `Self`, no `User`: unknown
            return None, None

    def magic_name(self) -> str | None:
        """This host's MagicDNS name (lowercase, no trailing dot), None when unknown."""
        try:
            return self._self_info()[1]
        except Exception:
            return None

    def _allowed_set(self) -> frozenset | None:
        if self._explicit:
            return self._explicit
        login = self._self_info()[0]
        return frozenset([login]) if login else None

    # --- the decision -------------------------------------------------------------------------
    def check(self, peer_ip) -> Verdict:
        try:
            ip = _peer_ip(peer_ip)
            if ip is None or not is_tailnet_ip(ip):
                return Verdict(False, "not_tailnet_peer")
            hit = self._cached(ip)
            if hit is not None:
                return hit
            with self._peer_lock(ip):
                hit = self._cached(ip)
                if hit is not None:
                    return hit
                try:
                    v = self._whois(ip)
                except Exception:
                    v = Verdict(False, "gate_error", node_ip=ip)
                self._store(ip, v)
                return v
        except Exception:
            return Verdict(False, "gate_error")

    def _whois(self, ip: str) -> Verdict:
        allowed = self._allowed_set()
        if allowed is None:
            return Verdict(False, "self_login_unknown", node_ip=ip, fresh=True)

        def deny(reason, **kw):
            return Verdict(False, reason, node_ip=ip, fresh=True, **kw)

        try:
            d = json.loads(self._run(["whois", "--json", ip]))
        except (TailscaleError, ValueError):
            return deny("whois_failed")
        node = d.get("Node") if isinstance(d, dict) else None
        if not isinstance(node, dict):
            return deny("whois_failed")
        prof = d.get("UserProfile")
        login = prof.get("LoginName") if isinstance(prof, dict) else None
        name = node.get("ComputedName")
        if not (isinstance(name, str) and name.strip()):
            name = node.get("Name")
        name = name.strip().rstrip(".") if isinstance(name, str) else None
        name = name or None
        if node.get("Tags") or _norm_login(login) in ("", "tagged-devices"):
            return deny("tagged_node", login=login if isinstance(login, str) else None, node=name)
        if _norm_login(login) not in allowed:
            return deny("login_not_allowed", login=login, node=name)
        return Verdict(True, "ok", login=login, node=name, node_ip=ip, fresh=True)

    # --- the cache ----------------------------------------------------------------------------
    def _cached(self, ip: str) -> Verdict | None:
        with self._lock:
            ent = self._cache.get(ip)
            if ent is not None and self._clock() < ent[0]:
                return dataclasses.replace(ent[1], fresh=False)
        return None

    def _store(self, ip: str, v: Verdict) -> None:
        now = self._clock()
        with self._lock:
            self._cache.pop(ip, None)           # re-insert: dict order is store order
            if len(self._cache) >= CACHE_MAX:
                for k in [k for k, (exp, _) in self._cache.items() if exp <= now]:
                    del self._cache[k]
            while len(self._cache) >= CACHE_MAX:
                del self._cache[next(iter(self._cache))]
            self._cache[ip] = (now + (ALLOWED_TTL_S if v.allowed else DENIED_TTL_S), v)

    @contextlib.contextmanager
    def _peer_lock(self, ip: str):
        with self._lock:
            ent = self._locks.setdefault(ip, [threading.Lock(), 0])
            ent[1] += 1
        try:
            with ent[0]:
                yield
        finally:
            with self._lock:
                ent[1] -= 1
                if ent[1] == 0:
                    self._locks.pop(ip, None)
