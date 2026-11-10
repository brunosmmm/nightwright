"""Federation signed channel (SPEC-0529): HMAC helpers and `HubClient`. No relay or queue."""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
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


def federation_path(cfg) -> Path:
    return Path(cfg["config_dir"]) / FEDERATION_REF


def host_secret_ref(name: str) -> str:
    return f"secrets/hosts/{name}.key"


def host_token_path(cfg, name: str) -> Path:
    return Path(cfg["config_dir"]) / "secrets" / "hosts" / f"{name}.token"


def host_secret_path(cfg, name: str) -> Path:
    return Path(cfg["config_dir"]) / host_secret_ref(name)


def new_host_credential(*, write: bool = True) -> tuple[str, str]:
    token, secret = secrets.token_urlsafe(32), secrets.token_hex(32)
    return token, secret


def write_host_secrets(cfg, name: str, token: str, secret: str) -> None:
    tok_p = host_token_path(cfg, name)
    PJ.write_private(tok_p, token + "\n")
    try:
        PJ.write_private(host_secret_path(cfg, name), secret + "\n")
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
    PJ.write_private(federation_path(cfg), json.dumps(out, sort_keys=True) + "\n")


def _body_sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest() if body else EMPTY_BODY_SHA


def sign(secret: str, method: str, path: str, body: bytes, ts: int) -> str:
    """Lowercase hex HMAC for the request/response four-tuple."""
    line = f"{method.upper()}|{path}|{_body_sha(body)}|{ts}".encode()
    return hmac.new(secret.encode("utf-8"), line, hashlib.sha256).hexdigest()


def verify(secret: str, method: str, path: str, body: bytes, mac: str, ts: int,
           *, now: float | None = None) -> str | None:
    """Why the MAC is invalid, else None."""
    now = time.time() if now is None else now
    if abs(int(now) - int(ts)) > SKEW_S:
        return "stale signature"
    want = sign(secret, method, path, body, ts)
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
    return {"X-NW-Req-Ts": str(ts), "X-NW-Req-Sig": sign(secret, method, path, body, ts)}


def response_headers(secret: str, method: str, path: str, body: bytes, *,
                     now: float | None = None) -> dict[str, str]:
    ts = int(now if now is not None else time.time())
    return {"X-NW-Sig-Ts": str(ts), "X-NW-Sig": sign(secret, method, path, body, ts)}


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

    def _url(self, path: str) -> str:
        return self.record["url"].rstrip("/") + path

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
                         out: list, lock: threading.Lock) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        sock = socket.socket(family, stype, proto)
        try:
            sock.settimeout(remaining)
            sock.connect(sockaddr)
            with lock:
                out.append(("ok", sock))
        except OSError as e:
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
        threads: list[threading.Thread] = []
        for family, stype, proto, _canon, sockaddr in addrs:
            t = threading.Thread(
                target=self._try_connect_one,
                args=(family, stype, proto, sockaddr, deadline, results, lock),
                daemon=True,
            )
            t.start()
            threads.append(t)
        last: OSError | None = None
        seen = 0
        while time.monotonic() < deadline:
            with lock:
                pending = results[seen:]
                seen = len(results)
            for kind, val in pending:
                if kind == "ok":
                    return val
                last = val
            if seen >= len(addrs) and all(not t.is_alive() for t in threads):
                break
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
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
        try:
            if self.https:
                sock = self._connect()
                ctx = ssl.create_default_context()
                ssl_sock = ctx.wrap_socket(sock, server_hostname=self.host)
                sock = None
                conn = http.client.HTTPSConnection(self.host, self.port)
                conn.sock = ssl_sock
            else:
                conn = http.client.HTTPConnection(self.host, self.port)
                conn.sock = self._connect()
            conn.request(method, target, body=data, headers=headers)
            read_deadline = time.monotonic() + self.READ_TIMEOUT

            def _read_to() -> float:
                return max(0.001, read_deadline - time.monotonic())

            io_sock = conn.sock
            io_sock.settimeout(_read_to())
            resp = conn.getresponse()
            raw = b""
            while True:
                if read_deadline - time.monotonic() <= 0:
                    raise TimeoutError("timed out")
                if io_sock.fileno() != -1:
                    io_sock.settimeout(_read_to())
                chunk = resp.read(min(65536, MAX_RESPONSE_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw += chunk
                if len(raw) > MAX_RESPONSE_BYTES:
                    break
            hdrs = {k: v for k, v in resp.getheaders()}
            status = resp.status
        except (OSError, ssl.SSLError, http.client.HTTPException) as e:
            raise ConnectionError(f"{type(e).__name__}: {e}") from e
        finally:
            if conn is not None:
                conn.close()
            elif sock is not None:
                sock.close()
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ConnectionError("response too large")
        if status != 200:
            raise ConnectionError(f"HTTP {status}")
        ts_raw = self._header(hdrs, "X-NW-Sig-Ts")
        mac = self._header(hdrs, "X-NW-Sig")
        if (not ts_raw or not str(ts_raw).isascii() or not str(ts_raw).isdigit()
                or not mac):
            raise ConnectionError("missing response signature")
        why = verify(self.secret, method, target, raw, mac, int(ts_raw), now=self._now)
        if why:
            raise ConnectionError(why)
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
