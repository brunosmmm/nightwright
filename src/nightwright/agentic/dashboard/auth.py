"""Dashboard server auth (SPEC-0414, SPEC-0416, SPEC-0417, SPEC-0428): the bind gate, the strict
Host/Origin (DNS rebinding) allowlist, same-origin and CSRF checks for writes, and the tailnet
identity gate. A loopback bind needs no credential (mode `local`); an own tailnet address
(`tailscale ip`) is served in mode `whois`, where `Gate` (`tailscale whois`) decides every
request. Nothing here opens a socket. Not imported by `snapshot` or any queue-side module."""
from __future__ import annotations

import base64
import contextlib
import dataclasses
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import subprocess
import threading
import time


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
    `TailnetMissing`. `stdout` is what a non-zero exit printed (None otherwise)."""

    def __init__(self, msg, stdout=None):
        super().__init__(msg)
        self.stdout = stdout


class TailscaleNotInstalled(TailscaleError):
    """`tailscale` is not on PATH or not executable."""


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
        raise TailscaleNotInstalled(f"cannot run `tailscale`: {e}")
    if r.returncode != 0:
        raise TailscaleError(f"`tailscale {' '.join(args)}` exited {r.returncode}", r.stdout)
    return r.stdout


# SPEC-0432 Decision 4: why there is no tailnet address, and the fix
_BACKEND_REASON = {"NeedsLogin": "logged out", "NoState": "logged out",
                   "NeedsMachineAuth": "waiting for admin approval", "Stopped": "stopped",
                   "Starting": "starting"}
_REASON_HINT = {"logged out": "sudo tailscale up", "stopped": "sudo tailscale up",
                "tailscaled not running": "sudo systemctl start tailscaled",
                "tailscale not installed": "install Tailscale",
                "waiting for admin approval": "approve this machine in the Tailscale admin "
                                              "console"}


def tailnet_reason(run=run_tailscale) -> tuple[str, str | None]:
    """`(reason, hint)` for a host with no tailnet address (SPEC-0432), from `tailscale status
    --json` `BackendState`: `logged out`, `stopped`, `starting`, `tailscaled not running`,
    `tailscale not installed`, or `no tailnet address` when the backend says it is running.
    Never raises."""
    try:
        try:
            out = run(["status", "--json"])
        except TailscaleNotInstalled:
            return "tailscale not installed", _REASON_HINT["tailscale not installed"]
        except TailscaleError as e:
            out = e.stdout or ""
        try:
            state = json.loads(out).get("BackendState") if out.strip() else None
        except (ValueError, AttributeError):
            state = None
        if not isinstance(state, str):
            reason = "tailscaled not running"
        else:
            reason = _BACKEND_REASON.get(state, "no tailnet address")
        return reason, _REASON_HINT.get(reason)
    except Exception:                           # noqa: BLE001 - a reason is best effort
        return "no tailnet address", None


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
    `BindRefused` for everything refused. An own tailnet address is served behind the whois
    gate (SPEC-0428)."""
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
    return host, port


def host_values(bound_host: str, port: int, mode: str | None = None,
                magic: str | None = None) -> set[str]:
    """The `Host` values accepted (SPEC-0428), all with the port: the bound address (an IPv6 one
    in brackets); on a loopback bind also `127.0.0.1`, `localhost` and `[::1]`; on a tailnet bind
    also the host's MagicDNS name and its first label when known."""
    mode = mode or bind_mode(bound_host)
    h = f"[{bound_host}]" if ":" in bound_host else bound_host
    names = {h}
    if mode == "local":
        names |= {"127.0.0.1", "localhost", "[::1]"}
    dns = _dns_name(magic)
    if dns and mode != "local":
        names |= {dns, dns.split(".", 1)[0]}
    return {f"{n}:{port}".lower() for n in names}


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
    """`"local"` for a loopback bind host (no credential, SPEC-0428), `"whois"` for every other
    host string (SPEC-0417)."""
    ip = _ip(host) if isinstance(host, str) else None
    return "local" if ip is not None and _unmapped(ip).is_loopback else "whois"


def check_same_origin(headers, allowed: set[str]) -> str | None:
    """SPEC-0428 for a state-changing request: an allowed `Origin`, or with no `Origin` a
    `Referer` on an allowed host; the audit reason (`bad_origin` / `no_origin`), else None."""
    origin = headers.get("Origin")
    if origin is not None:
        o = origin.strip().lower()
        ok = o.startswith("http://") and o[len("http://"):] in allowed
        return None if ok else "bad_origin"
    ref = headers.get("Referer")
    if ref is None:
        return "no_origin"
    r = ref.strip().lower()
    if not r.startswith("http://"):
        return "bad_origin"
    host = r[len("http://"):].split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    return None if host in allowed else "bad_origin"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class Csrf:
    """SPEC-0428 per-page CSRF values: `<nonce>.<mac>`, `mac = HMAC-SHA256(secret, "csrf" |
    nonce | identity_key)`. Stateless: a value is valid for this secret's (process's) life and
    only for the identity it was minted for. `sign` / `verify` are the same MAC for other
    purposes (SPEC-0429's plan token)."""

    def __init__(self, secret: bytes | None = None):
        self._secret = secret or secrets.token_bytes(32)

    def sign(self, *parts: str) -> str:
        msg = "\x00".join(parts).encode("utf-8", "surrogatepass")
        return _b64(hmac.new(self._secret, msg, hashlib.sha256).digest())

    def verify(self, mac, *parts: str) -> bool:
        return isinstance(mac, str) and hmac.compare_digest(
            mac.encode("ascii", "replace"), self.sign(*parts).encode("ascii"))

    def mint(self, identity_key: str) -> str:
        nonce = _b64(secrets.token_bytes(16))
        return f"{nonce}.{self.sign('csrf', nonce, identity_key)}"

    def check(self, value, identity_key: str) -> bool:
        if not isinstance(value, str) or len(value) > 200 or value.count(".") != 1:
            return False
        nonce, mac = value.strip().split(".")
        return bool(nonce) and self.verify(mac, "csrf", nonce, identity_key)


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
