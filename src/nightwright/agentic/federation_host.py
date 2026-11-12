"""`nw serve --federate` (SPEC-0529): signed `GET /v1/status` only."""
from __future__ import annotations

import hmac
import http.server
import json
import os
import re
import socket
import ssl
import threading
import time
from urllib.parse import parse_qs, urlsplit

from . import events as E
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
                       if isinstance(item, dict) and isinstance(item.get("project"), str)
                       and item["project"]})
    return {"schema": "nw.federation.status.v1", "projects": projects, "campaigns": campaigns}


class BadOffset(ValueError):
    """The requested byte offset is not an event-line boundary of the current file."""


_INT_RE = re.compile(r"-?[0-9]{1,18}")
_MAX_FIRST_LINE = F.MAX_RESPONSE_BYTES // 2   # a first line past this cannot come back in one response


def read_events_page(path, offset: int, limit: int) -> tuple[list[dict], int]:
    """SPEC-0530: up to `limit` complete `events.jsonl` lines from byte `offset`, as decoded objects,
    and the offset after the lines consumed. Only the byte before `offset` is read ahead of it;
    a trailing partial line is neither returned nor consumed; a line that is not one JSON object
    is consumed and omitted. The first line is returned even when it alone exceeds
    `F.EVENTS_MAX_BYTES`, so a cursor can always pass it; one too large for the hub's response cap
    is consumed unread and omitted. Memory use is bounded by one capped line."""
    try:
        f = open(path, "rb")
    except FileNotFoundError:
        if offset != 0:
            raise BadOffset(f"offset {offset} past the end") from None
        return [], 0
    with f:
        size = os.fstat(f.fileno()).st_size
        if offset > size:
            raise BadOffset(f"offset {offset} past the end")
        if offset > 0:
            f.seek(offset - 1)
            if f.read(1) != b"\n":
                raise BadOffset(f"offset {offset} is not at a line start")
        f.seek(offset)
        remaining = size - offset
        out: list[dict] = []
        consumed = lines = 0
        while lines < limit and consumed < remaining:
            first = lines == 0
            # Never hold more than one capped line: the first line may exceed EVENTS_MAX_BYTES
            # (so a cursor can pass it) up to what the hub can read back; later lines stop at the cap.
            cap = _MAX_FIRST_LINE if first else F.EVENTS_MAX_BYTES - consumed
            raw = f.readline(min(remaining - consumed, cap + 1))
            if first and len(raw) > cap:
                # Too large for the hub to read in one response: consume it unread, omit it.
                skipped = len(raw)
                while not raw.endswith(b"\n") and consumed + skipped < remaining:
                    raw = f.readline(min(remaining - consumed - skipped, 65536))
                    skipped += len(raw)
                if not raw.endswith(b"\n"):
                    break
                consumed += skipped
                lines += 1
                continue
            if not raw.endswith(b"\n"):
                break
            if lines and consumed + len(raw) > F.EVENTS_MAX_BYTES:
                break
            consumed += len(raw)
            lines += 1
            try:
                obj = json.loads(raw)
            except (ValueError, RecursionError):
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out, offset + consumed


def _events_args(query: str) -> tuple[int, int]:
    """`(offset, limit)` from a query string; raises BadOffset for a bad `offset`."""
    q = parse_qs(query, keep_blank_values=True)
    offset = 0
    if "offset" in q:
        raw = q["offset"][0]
        if not _INT_RE.fullmatch(raw) or raw.startswith("-"):
            raise BadOffset("offset is not a non-negative integer")
        offset = int(raw)
    limit = F.EVENTS_LIMIT
    raw = (q.get("limit") or [None])[0]
    if raw is not None and _INT_RE.fullmatch(raw) and 1 <= int(raw) <= F.EVENTS_LIMIT:
        limit = int(raw)
    return offset, limit


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
        # Private test seam (SPEC-0595): {(METHOD, route): fn(body) -> bytes} signed-200 routes.
        # The host serves none; tests register e.g. ("POST", "/v1/_test_echo").
        self.extra_routes: dict = {}
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
        try:
            self._dispatch(self.command)
        except ValueError:
            self.close_connection = True
            self._reject_bad_request(self.command)

    def _reject_bad_request(self, method: str) -> None:
        fed = F.read_federation(self.server.cfg)
        if fed is None:
            self._json_error(401, "unauthorized", signed=False, method=method, path="/",
                             secret=None)
            return
        self._json_error(401, "unauthorized", signed=True, method=method, path="/",
                         secret=fed["secret"])

    def _path(self) -> str:
        parts = urlsplit(self.path)
        return parts.path if not parts.query else f"{parts.path}?{parts.query}"

    def _read_body(self) -> bytes | None:
        raw = self.headers.get("Content-Length")
        s = str(raw) if raw is not None else ""
        if not s or not (s.isascii() and s.isdigit()):
            return b""
        n = int(s)
        if n > MAX_BODY_BYTES:
            return None
        if n <= 0:
            return b""
        try:
            return self.rfile.read(n)
        except (TimeoutError, OSError):
            return None

    def _write(self, status: int, data: bytes, *, signed: bool, method: str, path: str,
               secret: str | None, now: float | None = None) -> None:
        self.close_connection = True
        wire = b"" if method.upper() == "HEAD" else data
        self.send_response(status)
        if wire:
            self.send_header("Content-Type", "application/json")
        if signed and secret:
            hdrs = F.response_headers(secret, method, path, wire, status=status, now=now)
            for k in ("X-NW-Sig-Ts", "X-NW-Sig", F.SIG_V_HEADER):
                self.send_header(k, hdrs[k])
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
        try:
            path = self._path()
        except ValueError:
            raise ValueError("bad request target") from None
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
        # SPEC-0595: every authenticated request carries the signature-version marker; an old
        # client's four-field MAC would fail the MAC check below anyway.
        if self.headers.get(F.SIG_V_HEADER) != F.SIG_VERSION:
            self._json_error(401, "sig_version", signed=True, method=method, path=path,
                             secret=secret_key)
            return
        # Defence in depth (responses are domain-separated from requests since SPEC-0595): every
        # data route is bodyless, so refuse a GET/HEAD that carries a body before the MAC check.
        if method in ("GET", "HEAD") and (
                self.headers.get("Transfer-Encoding") is not None
                or (clen and str(clen).isascii() and str(clen).isdigit() and int(clen) > 0)):
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
        if F.verify(secret_key, method, path, body, mac or "", int(ts_s), direction="req"):
            self._json_error(401, "unauthorized", signed=True, method=method, path=path,
                             secret=secret_key)
            return
        try:
            route = urlsplit(self.path).path
        except ValueError:
            raise ValueError("bad request target") from None
        extra = self.server.extra_routes.get((method, route))
        if extra is not None:
            self._write(200, extra(body), signed=True, method=method, path=path,
                        secret=secret_key)
            return
        if route == "/v1/status" and method == "GET":
            try:
                doc = status_document(self.server.cfg)
            except Exception:
                self._json_error(500, "internal_error", signed=True, method=method, path=path,
                                 secret=secret_key)
                return
            data = json.dumps(doc, separators=(",", ":")).encode()
            self._write(200, data, signed=True, method=method, path=path, secret=secret_key)
            return
        if route == "/v1/events" and method == "GET":
            try:
                offset, limit = _events_args(urlsplit(self.path).query)
                evs, nxt = read_events_page(E.events_path(self.server.cfg), offset, limit)
            except BadOffset:
                self._json_error(409, "bad_offset", signed=True, method=method, path=path,
                                 secret=secret_key)
                return
            except Exception:
                self._json_error(500, "internal_error", signed=True, method=method, path=path,
                                 secret=secret_key)
                return
            doc = {"schema": "nw.federation.events.v1", "events": evs, "next_offset": nxt}
            data = json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            self._write(200, data, signed=True, method=method, path=path, secret=secret_key)
            return
        if route in ("/v1/status", "/v1/events"):
            self._json_error(405, "method_not_allowed", signed=True, method=method, path=path,
                             secret=secret_key)
            return
        self._json_error(404, "not_found", signed=True, method=method, path=path, secret=secret_key)
