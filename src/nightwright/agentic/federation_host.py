"""`nw serve --federate` (SPEC-0529): signed `GET /v1/status` only."""
from __future__ import annotations

import hmac
import http.server
import json
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

from . import federation as F
from . import pull_jobs as PJ

DEFAULT_BIND = "127.0.0.1:8792"
HANDLER_TIMEOUT_S = PJ.HANDLER_TIMEOUT_S
MAX_BODY_BYTES = PJ.MAX_FRAMES_BYTES


def status_document(cfg) -> dict:
    from . import queue as Q
    payload = Q.queue_payload(cfg)
    items = payload.get("items") or []
    campaigns = []
    for item in items:
        if not isinstance(item, dict):
            continue
        campaigns.append({k: item.get(k) for k in ("campaign", "project", "state", "spec_id", "kind")})
    projects = sorted({item.get("project") for item in items
                       if isinstance(item.get("project"), str) and item["project"]})
    return {"schema": "nw.federation.status.v1", "projects": projects, "campaigns": campaigns}


def make_federate_server(cfg, bind: str = DEFAULT_BIND, *, tls_cert=None, tls_key=None,
                         client_ca=None) -> FederateServer:
    host, port = PJ.split_bind(bind)
    if bool(tls_cert) != bool(tls_key):
        raise ValueError("--tls-cert and --tls-key go together")
    if client_ca:
        raise ValueError("--client-ca is not supported with --federate")
    why = PJ.check_bind(host, bool(tls_cert))
    if why:
        raise ValueError(f"refusing to bind: {why}")
    if not F.read_federation(cfg):
        raise ValueError(f"missing or invalid {F.federation_path(cfg)}")
    srv = FederateServer(cfg, (host, port))
    if tls_cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(tls_cert, tls_key)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
    return srv


class FederateServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, cfg, addr):
        self.cfg = cfg
        self.stopping = threading.Event()
        if ":" in addr[0]:
            self.address_family = socket.AF_INET6
        super().__init__(addr, _Handler)

    def finish_request(self, request, client_address):
        request.settimeout(HANDLER_TIMEOUT_S)
        if isinstance(request, ssl.SSLSocket):
            try:
                request.do_handshake()
            except (OSError, ValueError):
                return
        super().finish_request(request, client_address)

    def shutdown(self):
        self.stopping.set()
        super().shutdown()


class _Handler(http.server.BaseHTTPRequestHandler):
    server: FederateServer
    protocol_version = "HTTP/1.1"
    timeout = HANDLER_TIMEOUT_S

    def log_message(self, fmt, *args):
        pass

    def handle_one_request(self):
        try:
            self.raw_requestline = self.rfile.readline(65537)
        except TimeoutError:
            self.close_connection = True
            return
        if len(self.raw_requestline) > 65536:
            self.close_connection = True
            return
        if not self.raw_requestline:
            self.close_connection = True
            return
        if not self.parse_request():
            return
        self._dispatch(self.command)

    def _path(self) -> str:
        parts = urlsplit(self.path)
        return parts.path if not parts.query else f"{parts.path}?{parts.query}"

    def _read_body(self) -> bytes | None:
        raw = self.headers.get("Content-Length")
        if not raw or not raw.isdigit():
            return b""
        n = int(raw)
        if n > MAX_BODY_BYTES:
            return None
        return self.rfile.read(n) if n > 0 else b""

    def _write(self, status: int, data: bytes, *, signed: bool, method: str, path: str,
               secret: str | None, now: float | None = None) -> None:
        self.close_connection = True
        wire = b"" if method.upper() == "HEAD" else data
        self.send_response(status)
        if wire:
            self.send_header("Content-Type", "application/json")
        if signed and secret:
            hdrs = F.response_headers(secret, method, path, wire, now=now)
            self.send_header("X-NW-Sig-Ts", hdrs["X-NW-Sig-Ts"])
            self.send_header("X-NW-Sig", hdrs["X-NW-Sig"])
        self.send_header("Content-Length", str(len(wire)))
        self.send_header("Connection", "close")
        self.end_headers()
        if wire:
            self.wfile.write(wire)

    def _json_error(self, status: int, error: str, *, signed: bool, method: str, path: str,
                    secret: str | None) -> None:
        data = json.dumps({"error": error}, separators=(",", ":")).encode()
        self._write(status, data, signed=signed, method=method, path=path, secret=secret)

    def _dispatch(self, method: str) -> None:
        path = self._path()
        fed = F.read_federation(self.server.cfg)
        if fed is None:
            self._json_error(401, "unauthorized", signed=False, method=method, path=path,
                             secret=None)
            return
        tok = fed["token"]
        secret_key = fed["secret"]
        auth = self.headers.get("Authorization") or ""
        token = auth[7:].strip() if auth.startswith("Bearer ") else None
        clen = self.headers.get("Content-Length")
        if clen and str(clen).isascii() and str(clen).isdigit() and int(clen) > 0:
            self.close_connection = True
        try:
            tok_ok = bool(token) and hmac.compare_digest(token, tok)
        except (TypeError, ValueError):
            tok_ok = False
        if not tok_ok:
            self._json_error(401, "unauthorized", signed=True, method=method, path=path,
                             secret=secret_key)
            return
        ts_raw = self.headers.get("X-NW-Req-Ts") or self.headers.get("x-nw-req-ts")
        ts_s = str(ts_raw) if ts_raw is not None else ""
        if not ts_s.isascii() or not ts_s.isdigit() or (len(ts_s) > 1 and ts_s[0] == "0"):
            self._json_error(401, "unauthorized", signed=True, method=method, path=path,
                             secret=secret_key)
            return
        body = self._read_body()
        if body is None:
            self._json_error(401, "unauthorized", signed=True, method=method, path=path,
                             secret=secret_key)
            return
        mac = self.headers.get("X-NW-Req-Sig") or self.headers.get("x-nw-req-sig")
        if F.verify(secret_key, method, path, body, mac or "", int(ts_s)):
            self._json_error(401, "unauthorized", signed=True, method=method, path=path,
                             secret=secret_key)
            return
        route = urlsplit(self.path).path
        if route == "/v1/status" and method == "GET":
            doc = status_document(self.server.cfg)
            data = json.dumps(doc, separators=(",", ":")).encode()
            self._write(200, data, signed=True, method=method, path=path, secret=secret_key)
            return
        if route == "/v1/status":
            self._json_error(405, "method_not_allowed", signed=True, method=method, path=path,
                             secret=secret_key)
            return
        self._json_error(404, "not_found", signed=True, method=method, path=path, secret=secret_key)
