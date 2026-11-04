"""The dashboard HTTP server (SPEC-0414): `nw serve --dashboard`, loopback only, bearer-token
auth, `GET /api/snapshot` from the shared `SnapshotCache`. Read-only: it writes only under
`agentic/dashboard/` (`local.token`, `audit.jsonl`, `server.json`). stdlib only. Not imported by
any queue-side module."""
from __future__ import annotations

import contextlib
import hmac
import http.server
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .. import store
from . import auth as A
from . import audit as AU
from . import dashboard_dir
from . import stream as ST
from .snapshot import SnapshotCache

DEFAULT_BIND = f"127.0.0.1:{A.DEFAULT_PORT}"
HANDLER_TIMEOUT_S = 30.0            # a connection silent this long is closed (tests patch it)
DRAIN_MAX = 64 * 1024               # a refused request's body is read (and dropped) up to this
SERVER_SCHEMA = "nw.dashboard.server.v1"


class DashboardServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, cfg, addr, cache, token):
        self.cfg, self.cache, self.token = cfg, cache, token
        self.serializations = 0                 # body encodings; the body is cached by `rev`
        self.hub = ST.Hub(cache, ST.SlotPool(ST.max_clients(cfg)))
        self.heartbeat_s = ST.HEARTBEAT_S       # stream knobs: tests inject shorter ones
        self.send_timeout_s = ST.SEND_TIMEOUT_S
        self.stream_auth_ok = lambda handler: True   # re-run every heartbeat_s; the tailnet
        #                                              slice plugs its whois check in here
        self._body = None
        self._body_lock = threading.Lock()
        if ":" in addr[0]:
            self.address_family = socket.AF_INET6
        super().__init__(addr, _Handler)
        self.hosts = A.host_values(addr[0], self.server_address[1])

    def finish_request(self, request, client_address):
        request.settimeout(HANDLER_TIMEOUT_S)
        super().finish_request(request, client_address)

    def handle_error(self, request, client_address):
        """A peer that resets or drops its connection is not an error worth a traceback."""
        if not isinstance(sys.exc_info()[1], OSError):
            super().handle_error(request, client_address)

    def server_close(self):
        self.hub.close()
        super().server_close()

    def snapshot_body(self) -> tuple[str, bytes]:
        """`(rev, JSON bytes)` of the current snapshot; encoded once per `rev`."""
        return self.encode(self.cache.get())

    def encode(self, doc: dict) -> tuple[str, bytes]:
        rev = doc["rev"]
        with self._body_lock:
            if self._body is None or self._body[0] != rev:
                self._body = (rev, json.dumps(doc).encode("utf-8"))
                self.serializations += 1
            return self._body


def make_server(cfg, bind: str | None = None, *, cache=None) -> DashboardServer:
    """The server, bound (not yet serving). `BindRefused` for a refused bind; the bind `OSError`
    propagates. `cache` is a test seam (a `SnapshotCache` with an injected clock or builder)."""
    host, port = A.validate_bind(bind or DEFAULT_BIND)
    dashboard_dir(cfg)
    token = A.local_token(cfg)
    return DashboardServer(cfg, (host, port), cache or SnapshotCache(cfg), token)


def _etag_matches(header: str | None, rev: str) -> bool:
    for tag in (header or "").split(","):
        tag = tag.strip()
        if tag == "*":
            return True
        if tag.startswith("W/"):
            tag = tag[2:]
        if tag == f'"{rev}"':
            return True
    return False


class _Handler(http.server.BaseHTTPRequestHandler):
    server: DashboardServer
    protocol_version = "HTTP/1.1"
    timeout = HANDLER_TIMEOUT_S

    def setup(self):
        self.timeout = HANDLER_TIMEOUT_S        # read at connection time, so tests can patch it
        super().setup()

    def log_message(self, fmt, *args):
        if os.environ.get("NW_SERVE_LOG"):
            super().log_message(fmt, *args)

    def _send(self, status, body=None, *, headers=(), ctype="application/json", raw=None):
        data = raw if raw is not None else (b"" if body is None else
                                            json.dumps(body).encode("utf-8"))
        self.send_response(status)
        if data:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-cache")
        for k, v in headers:
            self.send_header(k, v)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if data and self.command != "HEAD":
            self.wfile.write(data)

    def _refuse(self, status, reason, error, headers=()):
        AU.audit(self.server.cfg, reason=reason, status=status, method=self.command,
                 path=self.path, user_agent=self.headers.get("User-Agent"))
        self._send(status, {"error": error}, headers=headers)

    def _body_len(self):
        """Declared body size (0 for none); None when it cannot be read (chunked, malformed)."""
        if self.headers.get("Transfer-Encoding") is not None:
            return None
        raw = self.headers.get("Content-Length")
        if raw is None:
            return 0
        # over 18 digits is far beyond any body we read (and int() of 4300+ digits raises)
        return int(raw) if raw.isascii() and raw.isdigit() and len(raw) <= 18 else None

    def _token_ok(self) -> str | None:
        """None when the bearer token is right, else the audit reason."""
        auth = self.headers.get("Authorization")
        if auth is None:
            return "no_token"
        if not auth.startswith("Bearer "):
            return "bad_token"
        got = auth[7:].strip().encode("utf-8", "replace")
        return None if hmac.compare_digest(got, self.server.token.encode("utf-8")) \
            else "bad_token"

    def _handle(self):
        method = self.command
        n = self._body_len()
        if n is None or n > DRAIN_MAX:
            self.close_connection = True        # a body we will not read
        elif n:
            with contextlib.suppress(OSError):
                self.rfile.read(n)              # no unread bytes behind the response
        why = A.check_host_origin(self.headers, self.server.hosts)
        if why:
            return self._refuse(403, why, "bad host or origin")
        why = self._token_ok()
        if why:
            return self._refuse(401, why, "unauthorized",
                                headers=[("WWW-Authenticate", "Bearer")])
        if method != "GET":
            return self._refuse(405, "method_not_allowed", "method not allowed",
                                headers=[("Allow", "GET")])
        if n != 0:
            self.close_connection = True
            return self._send(413, {"error": "a GET takes no body"})
        parts = urlsplit(self.path)
        path = parts.path
        if path == "/api/stream":
            return self._stream((parse_qs(parts.query).get("rev") or [None])[0])
        if path != "/api/snapshot":
            return self._send(404, {"error": "not found"})
        try:
            rev, data = self.server.snapshot_body()
        except Exception as e:                  # noqa: BLE001 - keep serving
            return self._send(503, {"error": f"{type(e).__name__}: {e}"})
        etag = ("ETag", f'"{rev}"')
        if _etag_matches(self.headers.get("If-None-Match"), rev):
            return self._send(304, headers=[etag])
        self._send(200, raw=data, headers=[etag])

    def _stream(self, known_rev):
        """SSE: the snapshot on connect (unless `?rev=` is current), then on each `rev` change,
        and a `: hb` comment every `heartbeat_s`. `stream_auth_ok` is re-run every `heartbeat_s`
        whatever else was written. Ends on any socket error, an auth denial or server close; the
        slot is always released."""
        srv = self.server
        try:
            sub = srv.hub.subscribe(known_rev)
        except Exception as e:                  # noqa: BLE001 - keep serving
            return self._send(503, {"error": f"{type(e).__name__}: {e}"})
        if sub is None:
            return self._send(503, {"error": "too many streams"},
                              headers=[("Retry-After", "5")])
        self.close_connection = True
        try:
            self.connection.settimeout(srv.send_timeout_s)
            ST.set_keepalive(self.connection)
            self.send_response(200)
            for k, v in (("Content-Type", "text/event-stream; charset=utf-8"),
                         ("Cache-Control", "no-store"), ("X-Accel-Buffering", "no"),
                         ("X-Content-Type-Options", "nosniff"), ("Connection", "close")):
                self.send_header(k, v)
            self.end_headers()
            ST.write_retry(self.wfile)
            slice_s = max(0.01, min(srv.heartbeat_s, srv.hub.refresh_s))
            last_write = last_auth = time.monotonic()
            while not sub.closed:
                snap = sub.take(slice_s)
                if time.monotonic() - last_auth >= srv.heartbeat_s:
                    # on its own clock, so a busy queue (a snapshot every few seconds, each
                    # resetting `last_write`) cannot starve the revocation check
                    last_auth = time.monotonic()
                    if not srv.stream_auth_ok(self):
                        break
                if snap is not None:
                    rev, data = srv.encode(snap)
                    ST.write_event(self.wfile, "snapshot", rev, data)
                    last_write = time.monotonic()
                elif sub.closed or ST.peer_gone(self.connection):
                    break                       # free the slot now, not at the next write
                elif time.monotonic() - last_write >= srv.heartbeat_s:
                    ST.write_comment(self.wfile, "hb")
                    last_write = time.monotonic()
            if sub.error is not None:           # the hub cannot build snapshots any more
                ST.write_error(self.wfile, sub.error)
        except OSError:
            pass
        finally:
            srv.hub.unsubscribe(sub)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _handle


# ---- server.json -----------------------------------------------------------------------------

def _start_ticks(pid) -> int | None:
    """Field 22 of `/proc/<pid>/stat`, None where `/proc` is not available."""
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text()
        return int(raw.rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError, TypeError):
        return None


def server_json_path(cfg) -> Path:
    return dashboard_dir(cfg) / "server.json"


def write_server_json(cfg, server: DashboardServer) -> dict:
    host, port = server.server_address[:2]
    h = f"[{host}]" if ":" in host else host
    rec = {"schema": SERVER_SCHEMA, "pid": os.getpid(), "start_ticks": _start_ticks(os.getpid()),
           "started_at": store.now_iso(), "host": host, "port": port,
           "url": f"http://{h}:{port}", "auth": "token"}
    p = server_json_path(cfg)
    tmp = p.with_name(".server.json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, (json.dumps(rec) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, p)
    return rec


def remove_server_json(cfg) -> None:
    """Delete `server.json` only when it names this process."""
    p = server_json_path(cfg)
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(rec, dict) and rec.get("pid") == os.getpid():
        p.unlink(missing_ok=True)
