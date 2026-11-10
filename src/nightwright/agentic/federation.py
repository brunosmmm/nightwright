"""Federation signed channel (SPEC-0529): HMAC helpers and `HubClient`. No relay or queue."""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import secrets
import ssl
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import pull_jobs as PJ

FEDERATION_REF = "secrets/federation.json"
EMPTY_BODY_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
SKEW_S = 120


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
    PJ.write_private(host_token_path(cfg, name), token + "\n")
    PJ.write_private(host_secret_path(cfg, name), secret + "\n")


def drop_host_secrets(cfg, name: str) -> None:
    host_token_path(cfg, name).unlink(missing_ok=True)
    host_secret_path(cfg, name).unlink(missing_ok=True)


def read_host_token(cfg, name: str) -> str | None:
    try:
        s = host_token_path(cfg, name).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return s or None


def read_host_secret(cfg, name: str) -> str | None:
    try:
        s = host_secret_path(cfg, name).read_text(encoding="utf-8").strip()
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
    if not isinstance(mac, str) or not hmac.compare_digest(mac, want):
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
        self.port = u.port or (443 if self.https else 80)
        self.base = u.path.rstrip("/")
        token = read_host_token(cfg, record["name"])
        secret = read_host_secret(cfg, record["name"])
        if not token or not secret:
            raise ValueError("missing host credentials")
        self.token, self.secret = token, secret

    def _url(self, path: str) -> str:
        return self.record["url"].rstrip("/") + path

    def request(self, method: str, path: str, body: bytes = b"") -> tuple[int, bytes, dict]:
        data = body or b""
        headers = {"Authorization": f"Bearer {self.token}",
                   **request_headers(self.secret, method, path, data, now=self._now)}
        if data:
            headers["Content-Length"] = str(len(data))
        if self.https:
            conn = http.client.HTTPSConnection(self.host, self.port,
                                               timeout=self.CONNECT_TIMEOUT)
        else:
            conn = http.client.HTTPConnection(self.host, self.port,
                                              timeout=self.CONNECT_TIMEOUT)
        try:
            conn.request(method, self.base + path, body=data, headers=headers)
            conn.sock.settimeout(self.READ_TIMEOUT)
            resp = conn.getresponse()
            raw = resp.read()
            hdrs = {k: v for k, v in resp.getheaders()}
            status = resp.status
        except (OSError, ssl.SSLError) as e:
            raise ConnectionError(f"{type(e).__name__}: {e}") from e
        finally:
            conn.close()
        if status != 200:
            raise ConnectionError(f"HTTP {status}")
        ts_raw = hdrs.get("X-NW-Sig-Ts") or hdrs.get("x-nw-sig-ts")
        mac = hdrs.get("X-NW-Sig") or hdrs.get("x-nw-sig")
        if not ts_raw or not str(ts_raw).isdigit() or not mac:
            raise ConnectionError("missing response signature")
        why = verify(self.secret, method, path, raw, mac, int(ts_raw), now=self._now)
        if why:
            raise ConnectionError(why)
        return status, raw, hdrs

    def get_json(self, path: str) -> dict:
        _, raw, _ = self.request("GET", path)
        try:
            doc = json.loads(raw)
        except ValueError as e:
            raise ConnectionError("response is not JSON") from e
        if not isinstance(doc, dict):
            raise ConnectionError("response is not a JSON object")
        return doc
