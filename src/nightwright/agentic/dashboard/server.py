"""The dashboard HTTP server: `nw serve --dashboard`.

- SPEC-0414 (superseded by SPEC-0428 for auth): a stdlib `ThreadingHTTPServer`, the bind gate,
  refusals audited, `GET /api/snapshot` from the shared `SnapshotCache` with an ETag.
- SPEC-0418: the phone page (`GET /`, `/app.js`, `/app.css` via `assets.ROUTES`).
- SPEC-0428: no credential on a loopback bind; a strict `Host` allowlist and `Origin` check on
  every request; on a tailnet bind every request passes the whois `Gate` first; writes need JSON,
  same-origin and the per-page CSRF value embedded in `/`; no CORS header is ever sent.
- SPEC-0402: `GET /api/stream`, the shared snapshot over Server-Sent Events.
- SPEC-0429: `POST /api/escalations/<id>/plan|answer` through `escalations.plan_answer` /
  `apply_answer` (`answers`), audited.
- SPEC-0430: `GET /api/campaigns/<cid>` and the live run tail `GET /api/campaigns/<cid>/tail`.

The only queue-side write is `escalations.apply_answer`; the server's own files live under
`agentic/dashboard/` (`audit.jsonl`, `server.json`). Not imported by any queue-side module."""
from __future__ import annotations

import contextlib
import http.server
import json
import os
import re
import socket
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

from . import answers as ANS
from . import assets as AS
from . import auth as A
from . import audit as AU
from . import campaign as CP
from . import dashboard_dir, settings
from . import lifecycle as LC
from . import stream as ST
from .snapshot import SnapshotCache

DEFAULT_BIND = f"127.0.0.1:{A.DEFAULT_PORT}"
HANDLER_TIMEOUT_S = 30.0            # a connection silent this long is closed (tests patch it)
DRAIN_MAX = 64 * 1024               # a refused request's body is read (and dropped) up to this
POST_MAX = 16 * 1024                # a write's JSON body (SPEC-0429)
DEFAULT_TAIL_POLL_S = 1.0
_ESC_ROUTE = re.compile(r"^/api/escalations/([^/]+)/(plan|answer)$")
_CAMPAIGN_ROUTE = re.compile(r"^/api/campaigns/([^/]+)(/tail)?$")


def tail_poll_s(cfg) -> float:
    """`agentic.dashboard.tail_poll_s` (SPEC-0430); anything but a number in (0, 60] is 1.0."""
    ag = cfg.get("agentic")
    raw = ag.get("dashboard") if isinstance(ag, dict) else None
    v = raw.get("tail_poll_s") if isinstance(raw, dict) else None
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v <= 60
    return float(v) if ok else DEFAULT_TAIL_POLL_S


class DashboardServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, cfg, addr, cache, *, mode=None, gate=None, peer=None):
        self.cfg, self.cache = cfg, cache
        self.mode = mode or A.bind_mode(addr[0])
        if self.mode not in ("local", "whois"):
            raise ValueError(f"unknown auth mode {self.mode!r}")
        self.gate = None
        if self.mode == "whois":
            self.gate = gate or A.Gate(allowed_logins=settings(cfg)["allowed_logins"])
        self.peer = peer or (lambda address: address)   # test seam: never set by the CLI
        self.csrf = A.Csrf()                    # SPEC-0428: per-process secret
        self.serializations = 0                 # body encodings; the body is cached by `rev`
        self.hub = ST.Hub(cache, ST.SlotPool(ST.max_clients(cfg)))
        self.heartbeat_s = ST.HEARTBEAT_S       # stream knobs: tests inject shorter ones
        self.send_timeout_s = ST.SEND_TIMEOUT_S
        self.tail_poll_s = tail_poll_s(cfg)
        # re-run every heartbeat_s on an open stream: on a tailnet bind the whois gate (through
        # its per-peer cache), so a revoked device loses its stream (SPEC-0402 Decision 3)
        self.stream_auth_ok = lambda handler: handler.identity(audit=False) is not None
        self._body = None
        self._body_lock = threading.Lock()
        if ":" in addr[0]:
            self.address_family = socket.AF_INET6
        super().__init__(addr, _Handler)
        magic = self.gate.magic_name() if self.gate is not None else None
        self.hosts = A.host_values(addr[0], self.server_address[1], self.mode, magic)

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

    def client_count(self) -> int:
        """Open live streams (snapshot and tail; `server.json` `clients`)."""
        return self.hub.pool.used

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


def make_server(cfg, bind: str | None = None, *, cache=None, own=None, mode=None, gate=None,
                peer=None) -> DashboardServer:
    """The server, bound (not yet serving). `BindRefused` for a refused bind, `TailnetMissing`
    when the bind needs a tailnet this host lacks; the bind `OSError` propagates. Test seams,
    never passed by `serve_cmd`: `cache` (a `SnapshotCache` with an injected clock or builder),
    `own` (replaces `auth.own_tailnet_ips`), `mode` / `gate` / `peer` (run the whois mode on a
    loopback socket with a fake gate and an injected peer address)."""
    host, port = A.resolve_bind(bind or DEFAULT_BIND, own_fn=own or A.own_tailnet_ips)
    dashboard_dir(cfg)
    return DashboardServer(cfg, (host, port), cache or SnapshotCache(cfg), mode=mode,
                           gate=gate, peer=peer)


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
        self._who = None
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
        if not any(k == "Cache-Control" for k, _ in headers):
            self.send_header("Cache-Control", "no-cache")
        for k, v in headers:
            self.send_header(k, v)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if data and self.command != "HEAD":
            self.wfile.write(data)

    def _who_fields(self) -> dict:
        return self._who.audit() if self._who is not None else {}

    def _refuse(self, status, reason, error, headers=(), **extra):
        who = self._who_fields() or {"node_ip": self.server.peer(self.client_address[0])}
        if "esc_id" in extra:                   # a refused answer (SPEC-0429): its result too
            extra.setdefault("result", str(status))
        AU.audit(self.server.cfg, reason=reason, status=status, method=self.command,
                 path=self.path, user_agent=self.headers.get("User-Agent"), **who, **extra)
        self._send(status, {"error": error, "reason": reason}, headers=headers)

    def _body_len(self):
        """Declared body size (0 for none); None when it cannot be read (chunked, malformed)."""
        if self.headers.get("Transfer-Encoding") is not None:
            return None
        raw = self.headers.get("Content-Length")
        if raw is None:
            return 0
        # over 18 digits is far beyond any body we read (and int() of 4300+ digits raises)
        return int(raw) if raw.isascii() and raw.isdigit() and len(raw) <= 18 else None

    # ---- identity (SPEC-0428) ------------------------------------------------------------

    def identity(self, *, audit: bool = True):
        """The caller's `Identity`: `local` on a loopback bind; on a tailnet bind the whois
        verdict for the peer (cached by the gate). None, audited, when the gate denies."""
        srv = self.server
        peer = srv.peer(self.client_address[0])
        if srv.mode == "local":
            return ANS.Identity("local", "local", peer)
        v = srv.gate.check(peer)
        if not v.allowed:
            if audit:
                AU.audit(srv.cfg, reason=v.reason, status=403, method=self.command,
                         path=self.path, user_agent=self.headers.get("User-Agent"),
                         login=v.login, node=v.node, node_ip=v.node_ip or peer)
            return None
        return ANS.Identity(v.login, v.node, v.node_ip or peer)

    # ---- dispatch ------------------------------------------------------------------------

    def _handle(self):
        method = self.command
        n = self._body_len()
        body = b""
        if n is None or n > (POST_MAX if method == "POST" else DRAIN_MAX):
            self.close_connection = True        # a body we will not read
        elif n:
            with contextlib.suppress(OSError):
                body = self.rfile.read(n)       # no unread bytes behind the response
        why = A.check_host_origin(self.headers, self.server.hosts)
        if why:
            return self._refuse(403, why, "bad host or origin")
        self._who = self.identity()
        if self._who is None:
            return self._send(403, {"error": "this device is not allowed", "reason": "identity"})
        parts = urlsplit(self.path)
        path = parts.path
        # http.server folds a leading `//` into `/`; the request as sent is not a route
        sent = self.requestline.split(" ")[1:2]
        if sent and sent[0].startswith("//"):
            return self._send(404, {"error": "not found"})
        m = _ESC_ROUTE.match(path)
        if m:
            if method != "POST":
                return self._refuse(405, "method_not_allowed", "method not allowed",
                                    headers=[("Allow", "POST")])
            return self._post_answer(m.group(1), m.group(2), n, body)
        try:
            asset = AS.read_asset(path)
        except Exception as e:                  # noqa: BLE001 - a broken install must not kill us
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        if method != "GET":
            return self._refuse(405, "method_not_allowed", "method not allowed",
                                headers=[("Allow", "GET")])
        if n != 0:
            self.close_connection = True
            return self._refuse(413, "body_on_get", "a GET takes no body")
        if asset is not None:
            data, ctype = asset
            headers = [("Content-Security-Policy", AS.CSP)]
            if path == "/":
                data = AS.render_index(data, self.server.csrf.mint(self._who.key))
                headers.append(("Cache-Control", "no-store"))
            return self._send(200, raw=data, ctype=ctype, headers=headers)
        if path == "/api/stream":
            return self._stream((parse_qs(parts.query).get("rev") or [None])[0])
        if path == "/api/snapshot":
            return self._snapshot()
        m = _CAMPAIGN_ROUTE.match(path)
        if m:
            if m.group(2):
                q = parse_qs(parts.query)
                return self._tail(m.group(1), (q.get("run") or [None])[0],
                                  (q.get("offset") or ["0"])[0])
            return self._campaign(m.group(1))
        return self._send(404, {"error": "not found"})

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _handle

    # ---- reads -------------------------------------------------------------------------------

    def _snapshot(self):
        try:
            rev, data = self.server.snapshot_body()
        except Exception as e:                  # noqa: BLE001 - keep serving
            return self._send(503, {"error": f"{type(e).__name__}: {e}"})
        etag = ("ETag", f'"{rev}"')
        if _etag_matches(self.headers.get("If-None-Match"), rev):
            return self._send(304, headers=[etag])
        self._send(200, raw=data, headers=[etag])

    def _campaign(self, cid):
        try:
            doc = CP.build(self.server.cfg, cid)
        except Exception as e:                  # noqa: BLE001 - keep serving
            return self._send(503, {"error": f"{type(e).__name__}: {e}"})
        if doc is None:
            return self._send(404, {"error": f"no campaign {cid!r}"})
        self._send(200, doc)

    # ---- writes (SPEC-0428 checks, SPEC-0429 routes) ----------------------------------------

    def _post_answer(self, esc_id, action, n, raw):
        srv = self.server
        tag = {"esc_id": esc_id[:40]}
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if n is None or n > POST_MAX:
            return self._refuse(413, "body_too_large", f"the body is over {POST_MAX} bytes",
                                **tag)
        if ctype != "application/json":
            return self._refuse(415, "bad_content_type", "the body must be application/json",
                                **tag)
        why = A.check_same_origin(self.headers, srv.hosts)
        if why:
            return self._refuse(403, why, "a write must come from the dashboard page",
                                **tag)
        if not srv.csrf.check(self.headers.get("X-NW-CSRF"), self._who.key):
            return self._refuse(403, "bad_csrf", "the page's CSRF value is missing or stale: "
                                                 "reload the page", **tag)
        try:
            body = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, ValueError):
            body = None
        form = body.get("form") if isinstance(body, dict) else None
        base = {"action": action, "esc_id": esc_id[:40], "method": "POST", "path": self.path,
                "form": form if isinstance(form, str) else None,
                "user_agent": self.headers.get("User-Agent"), **self._who_fields()}
        fn = ANS.plan if action == "plan" else ANS.answer
        nostore = [("Cache-Control", "no-store")]
        try:
            out = fn(srv.cfg, esc_id, body, self._who, srv.csrf)
        except ANS.Refusal as r:
            AU.audit(srv.cfg, **base, result=r.result, status=r.status,
                     detail=r.body.get("error"))
            return self._send(r.status, r.body, headers=nostore)
        except OSError as e:
            AU.audit(srv.cfg, **base, result="500", status=500, detail=str(e))
            return self._send(500, {"error": f"{type(e).__name__}: {e}"}, headers=nostore)
        summary = (out.get("plan") or {}).get("summary") if action == "plan" else \
            out.get("summary")
        AU.audit(srv.cfg, **base, result="ok", status=200, summary=summary,
                 detail=out.get("event_error"))
        self._send(200, out, headers=nostore)

    # ---- streams (SPEC-0402, SPEC-0430) -------------------------------------------------------

    def _stream_headers(self):
        self.close_connection = True
        self.connection.settimeout(self.server.send_timeout_s)
        ST.set_keepalive(self.connection)
        self.send_response(200)
        for k, v in (("Content-Type", "text/event-stream; charset=utf-8"),
                     ("Cache-Control", "no-store"), ("X-Accel-Buffering", "no"),
                     ("X-Content-Type-Options", "nosniff"), ("Connection", "close")):
            self.send_header(k, v)
        self.end_headers()
        ST.write_retry(self.wfile)

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
        try:
            self._stream_headers()
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

    def _tail(self, cid, run, offset):
        """SPEC-0430: a run's transcript as `nw campaign tail -f` renders it, `event: lines`
        every `tail_poll_s` while lines arrive, `event: end` once the run has ended. Takes one
        slot of the shared pool."""
        srv = self.server
        if CP.read_state(srv.cfg, cid) is None:
            return self._send(404, {"error": f"no campaign {cid!r}"})
        off = int(offset) if isinstance(offset, str) and offset.isdigit() and \
            len(offset) <= 15 else 0
        try:
            if run is None:
                run = CP.current_run(srv.cfg, cid)[0]
                if run is None:
                    return self._send(404, {"error": f"no runs yet in {cid}"})
            if not CP.valid_cid(run):
                return self._send(404, {"error": f"no run {run!r}"})
            tailer = CP.Tailer(srv.cfg, cid, run, off)
        except (ValueError, OSError) as e:
            return self._send(404, {"error": str(e)})
        if not srv.hub.pool.acquire():
            return self._send(503, {"error": "too many streams"},
                              headers=[("Retry-After", "5")])

        def send(event, doc):
            data = json.dumps(doc, separators=(",", ":"), ensure_ascii=True).encode("ascii")
            ST.write_event(self.wfile, event, str(tailer.offset), data)

        try:
            self._stream_headers()
            send("start", {"run": tailer.run, "offset": tailer.offset, "header": tailer.header,
                           "status": tailer.status()})
            first = True
            last_write = last_auth = time.monotonic()
            while True:
                ended = tailer.ended()
                if not tailer.streaming:
                    lines = tailer.summary_lines() if ended else []
                else:
                    lines = tailer.poll(final=ended)
                if first:
                    lines, first = lines[-CP.BACKLOG_LINES:], False
                if lines:
                    send("lines", {"run": tailer.run, "offset": tailer.offset, "lines": lines})
                    last_write = time.monotonic()
                if ended:
                    send("end", {"run": tailer.run, "offset": tailer.offset,
                                 "status": tailer.status()})
                    break
                deadline = time.monotonic() + srv.tail_poll_s
                while time.monotonic() < deadline:
                    if ST.peer_gone(self.connection):
                        return
                    time.sleep(min(0.1, srv.tail_poll_s))
                if time.monotonic() - last_auth >= srv.heartbeat_s:
                    last_auth = time.monotonic()
                    if not srv.stream_auth_ok(self):
                        break
                if time.monotonic() - last_write >= srv.heartbeat_s:
                    ST.write_comment(self.wfile, "hb")
                    last_write = time.monotonic()
        except OSError:
            pass
        finally:
            srv.hub.pool.release()


# ---- server.json (the record itself lives in `lifecycle`, SPEC-0404) -------------------------

server_json_path = LC.server_json_path
remove_server_json = LC.remove_server_record


def write_server_json(cfg, server: DashboardServer, *, code_watch=False, code_sha=None,
                      bind=None) -> dict:
    host, port = server.server_address[:2]
    return LC.write_server_record(cfg, LC.build_record(
        server, code_watch=code_watch, code_sha=code_sha, bind=bind or f"{host}:{port}"))
