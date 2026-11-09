"""SPEC-0413 integration: the phone page's logic (static/app.js, driven by
tests/js/page_http.test.js under node) against a stub server that implements exactly the
"Contract with the server" section of the spec, over REAL HTTP and the REAL snapshot builder on
the SPEC-0401 fixture. The stub lives here, never in the package. Only `GET`s reach it."""
import hashlib
import json
import os
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from fixtures.dashboard_data import ACTIVE, seed
from nightwright.agentic import queue as Q
from nightwright.agentic import schemas as SC
from nightwright.agentic import store as S
from nightwright.agentic.dashboard import assets
from nightwright.agentic.dashboard import snapshot as SN
from waits import WAIT_S

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parent.parent
NODE_SCRIPT = "page_http.test.js"        # tests/js/page_http.test.js (named for affected_tests)
APP_JS = "app.js"                        # src/nightwright/agentic/dashboard/static/app.js


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class Stub:
    """GET /, /app.js, /app.css via `assets.read_asset` + CSP; GET /api/snapshot from a real
    SnapshotCache with ETag/304 and a 401/403 toggle; GET /__test/<cmd> for the test's control."""

    def __init__(self, fx):
        self.fx = fx
        self.clock = Clock()
        self.cache = SN.SnapshotCache(fx["cfg"], clock=self.clock)
        self.mode = "allow"
        self.requests = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", ctype="application/json", extra=()):
                self.send_response(code)             # the stdlib adds the Date header
                if body or code != 304:
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(body)))
                for k, v in extra:
                    self.send_header(k, v)
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_GET(self):
                stub.requests.append(("GET", self.path))
                path = self.path
                asset = assets.read_asset(path)
                if asset is not None:
                    data, ctype = asset
                    return self._send(200, data, ctype, [
                        ("Content-Security-Policy", assets.CSP), ("Cache-Control", "no-cache"),
                        ("X-Content-Type-Options", "nosniff")])
                if path == "/api/snapshot":
                    if stub.mode in ("401", "403"):
                        return self._send(int(stub.mode), json.dumps({"error": stub.mode}).encode())
                    doc = stub.cache.get()
                    etag = f'"{doc["rev"]}"'
                    if self.headers.get("If-None-Match") == etag:
                        return self._send(304, extra=[("ETag", etag)])
                    body = json.dumps(doc, default=str).encode()
                    return self._send(200, body, extra=[("ETag", etag), ("Cache-Control", "no-cache")])
                if path.startswith("/__test/"):
                    stub.control(path[len("/__test/"):])
                    return self._send(200, b"{}")
                return self._send(404, b'{"error": "not found"}')

            def _no(self):
                stub.requests.append((self.command, self.path))
                self._send(405, b"{}")

            do_POST = do_PUT = do_DELETE = do_PATCH = _no

        self.httpd = ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("NW_DASHBOARD_STUB_PORT", 0))
                                          if os.environ.get("NW_DASHBOARD_STUB_HOLD") else 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def control(self, cmd):
        if cmd == "ledger":
            cfg = self.fx["cfg"]
            st = S.read_state(cfg, ACTIVE)
            st["cost"]["total_usd"] = 4.5
            S.write_state(cfg, ACTIVE, st)
            S.append_event(cfg, ACTIVE, "cost", SC.with_defaults("cost", {
                "persona": "implementer", "delta_usd": 3.25, "total_usd": 4.5, "cap_usd": 10.0,
                "estimated": False}))
            self.clock.t += self.cache.refresh_s + 1
        elif cmd in ("deny401", "deny403"):
            self.mode = cmd[-3:]
        elif cmd == "allow":
            self.mode = "allow"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def stub(tmp_path):
    fx = seed(tmp_path)
    with Stub(fx) as s:
        yield s


def tree(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def run_node(stub, scenario):
    node = shutil.which("node")
    assert node, "node is required to run tests/js/page_http.test.js (the page's JS tests)"
    r = subprocess.run([node, str(ROOT / "tests" / "js" / NODE_SCRIPT), str(stub.port), scenario],
                       capture_output=True, text=True, timeout=WAIT_S)
    assert r.returncode == 0, f"{NODE_SCRIPT} failed:\n{r.stdout}\n{r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def get(stub, path, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{stub.port}{path}", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=WAIT_S) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_page_over_real_http_with_a_ledger_change(stub):
    dp = Q.dashboard_payload(stub.fx["cfg"])
    out = run_node(stub, "full")
    assert out["spend"] == f"spend today ${dp['totals']['spend_today_usd']:.2f}"
    assert "ESC-0001" in out["needs"] and "main_dirty" in out["needs"]
    assert "$1.25 / $10.00" in out["active_before"]
    assert out["second_status"] == 304 and out["rebuilt_on_304"] is False
    assert out["after_change_status"] == 200
    assert "$4.50 / $10.00" in out["active_after"]
    assert out["banner_401"].startswith("The dashboard refused this request")
    assert out["banner_403"].startswith("This device is not allowed")
    assert out["banner_after"] == ""
    assert out["statuses"][:2] == [200, 304] and 401 in out["statuses"] and 403 in out["statuses"]
    assert {m for m, _ in stub.requests} == {"GET"}


def test_assets_headers_and_404(stub):
    for path, (_name, ctype) in assets.ROUTES.items():
        status, headers, body = get(stub, path)
        assert status == 200 and body
        assert headers["Content-Type"] == ctype
        assert headers["Content-Security-Policy"] == assets.CSP
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Cache-Control"] == "no-cache"
        assert "Date" in headers
    for path in ("/index.html", "/static/app.js", "/app.js?x=1", "/../app.js", "/nope"):
        assert get(stub, path)[0] == 404


def test_snapshot_etag_and_304(stub):
    status, headers, body = get(stub, "/api/snapshot")
    rev = json.loads(body)["rev"]
    assert status == 200 and headers["ETag"] == f'"{rev}"' and "Date" in headers
    status, headers, body = get(stub, "/api/snapshot", {"If-None-Match": f'"{rev}"'})
    assert status == 304 and body == b"" and headers["ETag"] == f'"{rev}"'


def test_serving_the_page_writes_nothing(stub):
    data = stub.fx["data"]
    before = tree(data)
    for path in list(assets.ROUTES) + ["/api/snapshot"]:
        get(stub, path)
    out = run_node(stub, "readonly")
    assert out["second_status"] == 304 and out["banner_after"] == ""
    assert tree(data) == before
    assert {m for m, _ in stub.requests if not _.startswith("/__test/")} == {"GET"}


@pytest.mark.skipif(not os.environ.get("NW_DASHBOARD_STUB_HOLD"),
                    reason="manual: NW_DASHBOARD_STUB_HOLD=1 serves the stub until interrupted")
def test_manual_serve(stub):
    print(f"\nnightwright dashboard stub: http://127.0.0.1:{stub.port}/  (Ctrl-C to stop; "
          "GET /__test/deny401|deny403|allow toggles auth)", flush=True)
    threading.Event().wait()

