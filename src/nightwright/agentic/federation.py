"""Federation signed channel (SPEC-0529): HMAC helpers and `HubClient`. No relay or queue."""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import secrets
import socket
import ssl
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import pull_jobs as PJ
from . import workers as WK

FEDERATION_REF = "secrets/federation.json"
EMPTY_BODY_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
SKEW_S = 120
MAX_RESPONSE_BYTES = PJ.MAX_FRAMES_BYTES
SIG_V_HEADER = "X-NW-Sig-V"
SIG_VERSION = "2"
UNAUTHORIZED_BODY = b'{"error":"unauthorized"}'


def federation_path(cfg) -> Path:
    return Path(cfg["config_dir"]) / FEDERATION_REF


def host_secret_ref(name: str) -> str:
    return f"secrets/hosts/{name}.key"


def host_token_path(cfg, name: str) -> Path:
    return Path(cfg["config_dir"]) / "secrets" / "hosts" / f"{name}.token"


def host_secret_path(cfg, name: str) -> Path:
    return Path(cfg["config_dir"]) / host_secret_ref(name)


def new_host_credential() -> tuple[str, str]:
    return secrets.token_urlsafe(32), secrets.token_hex(32)


def _mkdir_private(d: Path) -> None:
    """`mkdir -p` where every directory this call creates is 0700 (`mkdir(mode=)` only covers the
    leaf, and the umask can widen it), so a fresh `secrets/` is not left at the umask default."""
    missing = []
    while not d.exists():
        missing.append(d)
        d = d.parent
    for m in reversed(missing):
        try:
            m.mkdir(mode=0o700)
        except FileExistsError:
            continue
        os.chmod(m, 0o700)


def _write_private_fsync(path: Path, text: str) -> None:
    """`PJ.write_private` plus an fsync of the temp file before the rename, so a crash cannot
    leave the registry naming a host whose secret file is empty."""
    _mkdir_private(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    except OSError:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    os.close(fd)
    os.replace(tmp, path)


def write_host_secrets(cfg, name: str, token: str, secret: str) -> None:
    tok_p = host_token_path(cfg, name)
    _write_private_fsync(tok_p, token + "\n")
    try:
        _write_private_fsync(host_secret_path(cfg, name), secret + "\n")
    except OSError:
        tok_p.unlink(missing_ok=True)
        raise


def drop_host_secrets(cfg, name: str) -> None:
    host_token_path(cfg, name).unlink(missing_ok=True)
    host_secret_path(cfg, name).unlink(missing_ok=True)


def _strip_one_trailing_newline(s: str) -> str:
    return s[:-1] if s.endswith("\n") else s


def read_host_token(cfg, name: str) -> str | None:
    try:
        s = _strip_one_trailing_newline(host_token_path(cfg, name).read_text(encoding="utf-8"))
    except OSError:
        return None
    return s or None


def read_host_secret(cfg, name: str) -> str | None:
    try:
        s = _strip_one_trailing_newline(host_secret_path(cfg, name).read_text(encoding="utf-8"))
    except OSError:
        return None
    return s or None


def read_federation(cfg) -> dict | None:
    p = federation_path(cfg)
    if not p.is_file():
        return None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    for k in ("hub_name", "host_name", "token", "secret"):
        if not isinstance(doc.get(k), str) or not doc[k]:
            return None
    return doc


def write_federation(cfg, doc: dict) -> None:
    keys = ("hub_name", "host_name", "token", "secret")
    out = {k: doc[k] for k in keys if k in doc}
    if set(out) != set(keys):
        raise ValueError("federation document needs hub_name, host_name, token, and secret")
    _write_private_fsync(federation_path(cfg), json.dumps(out, sort_keys=True) + "\n")


def _body_sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest() if body else EMPTY_BODY_SHA


def _mac_line(method: str, path: str, body: bytes, ts: int, *, direction: str,
              status: int | None) -> str:
    """The signed ASCII line (SPEC-0595): a direction tag, then the fields joined by `|`."""
    if direction == "req":
        if status is not None:
            raise ValueError("status is only for direction='resp'")
        return f"nw-req|{method.upper()}|{path}|{_body_sha(body)}|{ts}"
    if direction == "resp":
        if (not isinstance(status, int) or isinstance(status, bool)
                or not 100 <= status <= 599):
            raise ValueError("direction='resp' needs a status from 100 to 599")
        return f"nw-resp|{status}|{method.upper()}|{path}|{_body_sha(body)}|{ts}"
    raise ValueError("direction must be 'req' or 'resp'")


def sign(secret: str, method: str, path: str, body: bytes, ts: int, *, direction: str,
         status: int | None = None) -> str:
    """Lowercase hex HMAC-SHA256 over `nw-req|METHOD|PATH|BODYSHA|TS` (a request) or
    `nw-resp|STATUS|METHOD|PATH|BODYSHA|TS` (a response). `direction` is required; `status` goes
    with `"resp"` only, so neither MAC can verify in the other's place."""
    line = _mac_line(method, path, body, ts, direction=direction, status=status)
    return hmac.new(secret.encode("utf-8"), line.encode(), hashlib.sha256).hexdigest()


def verify(secret: str, method: str, path: str, body: bytes, mac: str, ts: int,
           *, direction: str, status: int | None = None, now: float | None = None) -> str | None:
    """Why the MAC is invalid, else None."""
    now = time.time() if now is None else now
    if abs(int(now) - int(ts)) > SKEW_S:
        return "stale signature"
    want = sign(secret, method, path, body, ts, direction=direction, status=status)
    try:
        good = isinstance(mac, str) and hmac.compare_digest(mac, want)
    except (TypeError, ValueError):
        good = False
    if not good:
        return "bad signature"
    return None


def request_headers(secret: str, method: str, path: str, body: bytes, *,
                    now: float | None = None) -> dict[str, str]:
    ts = int(now if now is not None else time.time())
    return {"X-NW-Req-Ts": str(ts),
            "X-NW-Req-Sig": sign(secret, method, path, body, ts, direction="req"),
            SIG_V_HEADER: SIG_VERSION}


def response_headers(secret: str, method: str, path: str, body: bytes, *, status: int,
                     now: float | None = None) -> dict[str, str]:
    ts = int(now if now is not None else time.time())
    return {"X-NW-Sig-Ts": str(ts),
            "X-NW-Sig": sign(secret, method, path, body, ts, direction="resp", status=status),
            SIG_V_HEADER: SIG_VERSION}


class HubClient:
    """HTTP client from hub to a federation host."""

    CONNECT_TIMEOUT = 2
    READ_TIMEOUT = 2

    def __init__(self, record: dict, cfg, *, now: float | None = None):
        self.record, self.cfg, self._now = record, cfg, now
        url = record.get("url")
        if not isinstance(url, str):
            raise ValueError("host record has no url")
        u = urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError(f"url {url!r} is not http(s)://HOST")
        self.https = u.scheme == "https"
        self.host = u.hostname
        self.port = u.port if u.port is not None else (443 if self.https else 80)
        self.base = u.path.rstrip("/")
        name = record.get("name")
        if not isinstance(name, str) or not WK.NAME_RE.fullmatch(name):
            raise ValueError(f"host name must match {WK.NAME_RE.pattern}")
        token = read_host_token(cfg, name)
        secret = read_host_secret(cfg, name)
        if not token or not secret:
            raise ValueError("missing host credentials")
        self.token, self.secret = token, secret

    def _target(self, path: str) -> str:
        return self.base + path if self.base else path

    def _header(self, hdrs: dict[str, str], name: str) -> str | None:
        low = name.lower()
        for k, v in hdrs.items():
            if k.lower() == low:
                return v
        return None

    def _resolve_addrs(self, deadline: float) -> list:
        holder: dict = {}
        exc_holder: dict = {}

        def worker() -> None:
            try:
                holder["addrs"] = socket.getaddrinfo(
                    self.host, self.port, type=socket.SOCK_STREAM)
            except OSError as e:
                exc_holder["e"] = e

        th = threading.Thread(target=worker, daemon=True)
        th.start()
        wait = deadline - time.monotonic()
        th.join(wait if wait > 0 else 0)
        if th.is_alive():
            raise ConnectionError("TimeoutError: timed out")
        if "e" in exc_holder:
            e = exc_holder["e"]
            raise ConnectionError(f"{type(e).__name__}: {e}") from e
        addrs = holder.get("addrs")
        if not addrs:
            raise ConnectionError("gaierror: no addresses")
        return addrs

    def _try_connect_one(self, family, stype, proto, sockaddr, deadline: float,
                         out: list, lock: threading.Lock, state: dict) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        sock = None
        try:
            sock = socket.socket(family, stype, proto)
            sock.settimeout(remaining)
            sock.connect(sockaddr)
            with lock:
                if not state["done"]:
                    out.append(("ok", sock))
                    return
            sock.close()  # the race was already decided: a late winner is closed, not leaked
        except OSError as e:
            if sock is not None:
                sock.close()
            with lock:
                out.append(("err", e))

    def _connect(self) -> socket.socket:
        deadline = time.monotonic() + self.CONNECT_TIMEOUT
        addrs = self._resolve_addrs(deadline)
        if len(addrs) == 1:
            family, stype, proto, _canon, sockaddr = addrs[0]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ConnectionError("TimeoutError: timed out")
            sock = socket.socket(family, stype, proto)
            try:
                sock.settimeout(remaining)
                sock.connect(sockaddr)
                return sock
            except OSError as e:
                sock.close()
                raise ConnectionError(f"{type(e).__name__}: {e}") from e
        results: list = []
        lock = threading.Lock()
        state = {"done": False}
        threads: list[threading.Thread] = []
        for family, stype, proto, _canon, sockaddr in addrs:
            t = threading.Thread(
                target=self._try_connect_one,
                args=(family, stype, proto, sockaddr, deadline, results, lock, state),
                daemon=True,
            )
            t.start()
            threads.append(t)
        winner: socket.socket | None = None
        last: OSError | None = None
        seen = 0
        try:
            while winner is None and time.monotonic() < deadline:
                with lock:
                    pending = results[seen:]
                    seen = len(results)
                for kind, val in pending:
                    if kind == "ok":
                        if winner is None:
                            winner = val
                        else:
                            val.close()
                    else:
                        last = val
                if winner is not None:
                    break
                if seen >= len(addrs) and all(not t.is_alive() for t in threads):
                    break
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        finally:
            with lock:
                state["done"] = True
                # Close every success the loop did not take (found between the last poll and now).
                for kind, val in results[seen:]:
                    if kind == "ok" and val is not winner:
                        val.close()
        if winner is not None:
            return winner
        if last is not None:
            raise ConnectionError(f"{type(last).__name__}: {last}") from last
        raise ConnectionError("TimeoutError: timed out")

    def request(self, method: str, path: str, body: bytes = b"") -> tuple[int, bytes, dict]:
        data = body or b""
        target = self._target(path)
        headers = {"Authorization": f"Bearer {self.token}",
                   **request_headers(self.secret, method, target, data, now=self._now)}
        if data:
            headers["Content-Length"] = str(len(data))
        conn = None
        sock = None
        live: list = []
        timed_out = threading.Event()

        def _expire() -> None:
            # One wall-clock budget for handshake, send, headers and body: a peer that drips
            # bytes inside each recv timeout is cut off by shutting the socket under it.
            timed_out.set()
            for s_ in list(live):
                try:
                    socket.socket.shutdown(s_, socket.SHUT_RDWR)
                except (OSError, ValueError):
                    pass

        watchdog = None
        raw = b""
        try:
            sock = self._connect()
            live.append(sock)
            watchdog = threading.Timer(self.READ_TIMEOUT, _expire)
            watchdog.daemon = True
            watchdog.start()
            if self.https:
                ctx = ssl.create_default_context()
                ssl_sock = ctx.wrap_socket(sock, server_hostname=self.host)
                sock = None
                live.append(ssl_sock)
                conn = http.client.HTTPSConnection(self.host, self.port)
                conn.sock = ssl_sock
            else:
                conn = http.client.HTTPConnection(self.host, self.port)
                conn.sock = sock
                sock = None
            conn.sock.settimeout(self.READ_TIMEOUT)
            conn.request(method, target, body=data, headers=headers)
            resp = conn.getresponse()
            while True:
                chunk = resp.read(min(65536, MAX_RESPONSE_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw += chunk
                if len(raw) > MAX_RESPONSE_BYTES:
                    break
            hdrs = {k: v for k, v in resp.getheaders()}
            status = resp.status
        except (OSError, ssl.SSLError, ValueError, http.client.HTTPException) as e:
            if timed_out.is_set():
                raise ConnectionError("TimeoutError: timed out") from e
            raise ConnectionError(f"{type(e).__name__}: {e}") from e
        finally:
            if watchdog is not None:
                watchdog.cancel()
            if conn is not None:
                conn.close()
            if sock is not None:
                sock.close()
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ConnectionError("response too large")
        ts_raw = self._header(hdrs, "X-NW-Sig-Ts")
        mac = self._header(hdrs, "X-NW-Sig")
        if status == 401 and ts_raw is None and mac is None and raw == UNAUTHORIZED_BODY:
            raise ConnectionError("host not enrolled")
        if self._header(hdrs, SIG_V_HEADER) != SIG_VERSION:
            raise ConnectionError("host uses an old signature version")
        if (not ts_raw or not str(ts_raw).isascii() or not str(ts_raw).isdigit()
                or not mac):
            raise ConnectionError("missing response signature")
        why = verify(self.secret, method, target, raw, mac, int(ts_raw),
                     direction="resp", status=status, now=self._now)
        if why:
            raise ConnectionError(why)
        if status != 200:
            raise ConnectionError(f"HTTP {status}")
        return status, raw, hdrs

    def get_json(self, path: str) -> dict:
        _, raw, _ = self.request("GET", path)
        try:
            doc = json.loads(raw)
        except (ValueError, RecursionError) as e:
            raise ConnectionError("response is not JSON") from e
        if not isinstance(doc, dict):
            raise ConnectionError("response is not a JSON object")
        return doc
