"""SPEC-0428: the whois identity gate wired into the running server. Real sockets: the server runs
in `whois` mode on a loopback socket with a fake `tailscale` run function and an injected peer
address (no real tailnet exists in CI); every route, static files included, is gated."""
import http.client
import json
import re
import socket
import threading

import pytest

import waits
from fixtures.dashboard_data import seed
from nightwright.agentic.dashboard import auth as A
from nightwright.agentic.dashboard import server as DS

pytestmark = pytest.mark.fast

PEER = "100.101.1.2"
MAGIC = "box.tail1234.ts.net"


class FakeTailscale:
    def __init__(self):
        self.whois = {"Node": {"ComputedName": "phone", "Tags": None},
                      "UserProfile": {"LoginName": "me@example.com"}}
        self.fail = False
        self.calls = []

    def __call__(self, args, timeout=5.0):
        self.calls.append(args)
        if args[:2] == ["status", "--json"]:
            return json.dumps({"Self": {"UserID": 7, "DNSName": MAGIC + "."},
                               "User": {"7": {"LoginName": "me@example.com"}}})
        if self.fail:
            raise A.TailscaleError("whois exited 1")
        return json.dumps(self.whois)


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "ALLOWED_TTL_S", 0.3)
    monkeypatch.setattr(A, "DENIED_TTL_S", 0.1)
    fx = seed(tmp_path)
    ts = FakeTailscale()
    srv = DS.make_server(fx["cfg"], "127.0.0.1:0", mode="whois", gate=A.Gate(run=ts),
                         peer=lambda addr: PEER)
    srv.heartbeat_s = 0.2
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    port = srv.server_address[1]

    def req(method, path, headers=None, body=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        try:
            c.request(method, path, body=body,
                      headers={"Host": host or f"127.0.0.1:{port}", **(headers or {})})
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()

    def audit():
        p = tmp_path / "data" / "agentic" / "dashboard" / "audit.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    yield {"srv": srv, "ts": ts, "req": req, "port": port, "audit": audit}
    srv.shutdown()
    waits.join(t, 10)
    srv.server_close()


def test_mode_and_hosts(ws):
    srv, port = ws["srv"], ws["port"]
    assert srv.mode == "whois"
    assert {f"{MAGIC}:{port}", f"box:{port}", f"127.0.0.1:{port}"} <= srv.hosts
    assert f"localhost:{port}" not in srv.hosts
    for h in (f"{MAGIC}:{port}", f"box:{port}"):
        assert ws["req"]("GET", "/api/snapshot", host=h)[0] == 200
    assert ws["req"]("GET", "/api/snapshot", host=f"localhost:{port}")[0] == 403


def test_allowed_login_is_served_and_its_csrf_is_bound_to_login_and_node_ip(ws):
    for path in ("/", "/app.js", "/app.css", "/api/snapshot"):
        assert ws["req"]("GET", path)[0] == 200, path
    st, page = ws["req"]("GET", "/")
    tok = re.search(rb'name="nw-csrf" content="([^"]+)"', page).group(1).decode()
    assert ws["srv"].csrf.check(tok, f"me@example.com|{PEER}")
    assert not ws["srv"].csrf.check(tok, "local")
    origin = f"http://127.0.0.1:{ws['port']}"
    post = lambda t: ws["req"]("POST", "/api/escalations/ESC-0001/plan",  # noqa: E731
                               {"Content-Type": "application/json", "Origin": origin,
                                "X-NW-CSRF": t}, json.dumps({"form": "choose", "choose": 1}))
    assert post(ws["srv"].csrf.mint("local"))[0] == 403                 # a loopback page's value
    st, body = post(tok)
    assert st == 200, body
    a = [x for x in ws["audit"]() if x["action"] == "plan"][-1]
    assert (a["login"], a["node"], a["node_ip"], a["result"]) == \
        ("me@example.com", "phone", PEER, "ok")


@pytest.mark.parametrize("change,reason", [
    (lambda ts: ts.whois["UserProfile"].update(LoginName="other@example.com"), "login_not_allowed"),
    (lambda ts: ts.whois["Node"].update(Tags=["tag:server"]), "tagged_node"),
    (lambda ts: setattr(ts, "fail", True), "whois_failed"),
])
def test_denied_peers_get_403_on_every_route_with_the_reason_and_device_audited(ws, change,
                                                                                 reason):
    change(ws["ts"])
    for path in ("/", "/app.js", "/api/snapshot", "/api/stream", "/api/campaigns/x"):
        st, body = ws["req"]("GET", path)
        assert st == 403 and b"<html" not in body.lower(), path
    st, _ = ws["req"]("POST", "/api/escalations/ESC-0001/plan",
                      {"Content-Type": "application/json"}, b"{}")
    assert st == 403
    rows = [a for a in ws["audit"]() if a.get("reason") == reason]
    assert len(rows) >= 6 and all(a["node_ip"] == PEER for a in rows)
    if reason == "login_not_allowed":
        assert rows[0]["login"] == "other@example.com" and rows[0]["node"] == "phone"


def test_an_open_stream_is_closed_once_the_gate_denies(ws):
    s = socket.create_connection(("127.0.0.1", ws["port"]), timeout=20)
    s.sendall(f"GET /api/stream HTTP/1.1\r\nHost: 127.0.0.1:{ws['port']}\r\n\r\n".encode())
    buf = b""
    while b"event: snapshot" not in buf:
        buf += s.recv(65536)
    assert buf.startswith(b"HTTP/1.1 200")
    ws["ts"].whois["UserProfile"]["LoginName"] = "revoked@example.com"
    s.settimeout(waits.WAIT_S)
    closed = []

    def drain():
        while True:
            try:
                d = s.recv(65536)
            except OSError:
                d = b""
            if not d:
                closed.append(True)
                return

    t = threading.Thread(target=drain, daemon=True)
    t.start()
    assert waits.until(lambda: closed), "the stream stayed open after the gate denied"
    assert waits.until(lambda: ws["srv"].hub.stats()["snapshot_clients"] == 0)
    s.close()


def test_no_cors_headers_on_any_response(ws):
    c = http.client.HTTPConnection("127.0.0.1", ws["port"], timeout=20)
    for method, path in (("GET", "/"), ("GET", "/api/snapshot"), ("OPTIONS", "/api/snapshot"),
                         ("GET", "/nope"), ("POST", "/api/escalations/ESC-0001/plan")):
        c.request(method, path, headers={"Host": f"127.0.0.1:{ws['port']}"})
        r = c.getresponse()
        r.read()
        assert not [k for k, _ in r.getheaders() if k.lower().startswith("access-control-")]
        if method == "OPTIONS":
            assert r.status == 405
    c.close()

