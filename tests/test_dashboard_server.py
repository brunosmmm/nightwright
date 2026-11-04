"""SPEC-0414: the dashboard server in a thread on 127.0.0.1:0 over real sockets, against the
seeded data dir."""
import http.client
import json
import threading

import pytest

from fixtures.dashboard_data import ACTIVE, seed
from test_dashboard_snapshot import check_schema, ledger_run_end
from nightwright.agentic.dashboard import auth as A
from nightwright.agentic.dashboard import server as DS
from nightwright.agentic.dashboard import snapshot as SN


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class Srv:
    def __init__(self, fx, cache=None):
        self.fx, self.cfg = fx, fx["cfg"]
        self.clock = Clock()
        self.cache = cache or SN.SnapshotCache(self.cfg, clock=self.clock)
        self.srv = DS.make_server(self.cfg, "127.0.0.1:0", cache=self.cache)
        self.port = self.srv.server_address[1]
        self.token = self.srv.token
        self.t = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05},
                                  daemon=True)
        self.t.start()

    def req(self, method="GET", path="/api/snapshot", headers=None, body=None, auth=True,
            host=True):
        h = {}
        if auth:
            h["Authorization"] = f"Bearer {self.token}"
        if host:
            h["Host"] = f"127.0.0.1:{self.port}"
        h.update(headers or {})
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for k, v in h.items():
                c.putheader(k, v)
            if body is not None:
                c.putheader("Content-Length", str(len(body)))
            c.endheaders(body)
            r = c.getresponse()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
        finally:
            c.close()

    def audit(self):
        p = self.cfg and __import__("pathlib").Path(self.cfg["data_dir"]) / \
            "agentic/dashboard/audit.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def close(self):
        self.srv.shutdown()
        self.t.join(10)
        self.srv.server_close()


@pytest.fixture
def s(tmp_path):
    fx = seed(tmp_path)
    srv = Srv(fx)
    yield srv
    srv.close()


def test_snapshot_200_with_etag_equal_to_rev(s):
    st, h, body = s.req()
    doc = json.loads(body)
    check_schema(doc)
    assert st == 200 and h["etag"] == '"' + doc["rev"] + '"'
    assert h["content-type"] == "application/json" and h["x-content-type-options"] == "nosniff"
    assert h["cache-control"] == "no-cache" and int(h["content-length"]) == len(body)


def test_conditional_get_hit_miss_star_weak_and_list(s):
    _, h, body = s.req()
    tag = h["etag"]
    for inm in (tag, "*", "W/" + tag, f'"zzz", {tag}', f'W/"zzz" , W/{tag}'):
        st, h2, b2 = s.req(headers={"If-None-Match": inm})
        assert (st, b2, h2["etag"]) == (304, b"", tag), inm
    st, _, b2 = s.req(headers={"If-None-Match": '"stale"'})
    assert st == 200 and b2 == body


def test_one_serialization_and_one_build_for_one_rev(s):
    _, _, b1 = s.req()
    _, _, b2 = s.req()
    s.req(headers={"If-None-Match": "*"})
    assert b1 == b2 and s.srv.serializations == 1 and s.cache.builds == 1


def test_ledger_only_change_gives_a_new_etag_after_refresh(s):
    _, h, _ = s.req()
    ledger_run_end(s.cfg, ACTIVE, 0.9)
    s.clock.t += 1
    assert s.req()[1]["etag"] == h["etag"]
    s.clock.t += s.cache.refresh_s
    st, h2, _ = s.req(headers={"If-None-Match": h["etag"]})
    assert st == 200 and h2["etag"] != h["etag"] and s.srv.serializations == 2


@pytest.mark.parametrize("hdrs", [{}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic x"},
                                  {"Authorization": "Bearer"}])
def test_auth_matrix_401(s, hdrs):
    st, h, _ = s.req(auth=False, headers=hdrs)
    assert st == 401 and h["www-authenticate"] == "Bearer"


def test_token_in_query_is_401(s):
    st, _, _ = s.req(path=f"/api/snapshot?token={s.token}", auth=False)
    assert st == 401
    (rec,) = s.audit()
    assert rec["reason"] == "no_token" and s.token not in json.dumps(rec)


@pytest.mark.parametrize("hdrs,host,reason", [
    ({}, "evil.example", "bad_host"),
    ({}, "127.0.0.1:1", "bad_host"),
    ({"Origin": "http://evil.example"}, True, "bad_origin"),
    ({"Origin": "null"}, True, "bad_origin"),
])
def test_host_origin_403_before_auth(s, hdrs, host, reason):
    h = dict(hdrs)
    if host is not True:
        h["Host"] = host
    st, _, _ = s.req(headers=h, host=host is True)           # a valid token is sent
    assert st == 403
    assert s.audit()[-1]["reason"] == reason


@pytest.mark.parametrize("cred", [None, "Bearer wrong-token", "Basic x"])
@pytest.mark.parametrize("hdrs,host,reason", [
    ({}, "evil.example", "bad_host"),
    ({}, "127.0.0.1:1", "bad_host"),
    ({"Origin": "http://evil.example"}, True, "bad_origin"),
    ({"Origin": "null"}, True, "bad_origin"),
])
def test_host_origin_403_comes_before_the_token_check(s, hdrs, host, reason, cred):
    """Host/Origin run first: with no token or a wrong one the answer is still 403, audited as
    bad_host/bad_origin, never 401 no_token/bad_token."""
    h = dict(hdrs)
    if host is not True:
        h["Host"] = host
    if cred:
        h["Authorization"] = cred
    st, rh, _ = s.req(headers=h, host=host is True, auth=False)
    assert st == 403 and "www-authenticate" not in rh
    (rec,) = s.audit()
    assert (rec["reason"], rec["status"]) == (reason, 403)


def test_unauthenticated_post_is_401_not_405(s):
    """The method check runs after authentication."""
    st, _, _ = s.req("POST", body=b"{}", auth=False)
    assert st == 401
    st, _, _ = s.req("POST", body=b"{}", auth=False, headers={"Authorization": "Bearer x"})
    assert st == 401
    assert [r["reason"] for r in s.audit()] == ["no_token", "bad_token"]


def test_ipv6_loopback_host_passes(tmp_path):
    import socket
    if not socket.has_ipv6:
        pytest.skip("no IPv6")
    fx = seed(tmp_path)
    try:
        srv = DS.make_server(fx["cfg"], "[::1]:0")
    except OSError:
        pytest.skip("::1 is not available")
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    try:
        port = srv.server_address[1]
        for host, want in ((f"[::1]:{port}", 200), (f"localhost:{port}", 200),
                           (f"127.0.0.1:{port}", 403)):
            c = http.client.HTTPConnection("::1", port, timeout=10)
            c.putrequest("GET", "/api/snapshot", skip_host=True, skip_accept_encoding=True)
            c.putheader("Host", host)
            c.putheader("Authorization", f"Bearer {srv.token}")
            c.endheaders()
            assert c.getresponse().status == want, host
            c.close()
    finally:
        srv.shutdown()
        t.join(10)
        srv.server_close()


def test_server_json_fields(tmp_path):
    fx = seed(tmp_path)
    s = Srv(fx)
    try:
        rec = DS.write_server_json(fx["cfg"], s.srv)
        on_disk = json.loads(DS.server_json_path(fx["cfg"]).read_text())
        assert on_disk == rec
        assert rec["schema"] == "nw.dashboard.server.v1"
        assert rec["url"] == f"http://127.0.0.1:{s.port}" and rec["port"] == s.port
        assert rec["host"] == "127.0.0.1" and rec["auth"] == "token"
        assert isinstance(rec["started_at"], str) and rec["started_at"]
        assert s.token not in json.dumps(rec)
    finally:
        s.close()


@pytest.mark.parametrize("how", ["huge", "chunked"])
def test_refused_request_with_an_undrainable_body_closes_the_connection(s, how):
    import socket
    head = [f"POST /api/snapshot HTTP/1.1", f"Host: 127.0.0.1:{s.port}"]     # no token: 401
    if how == "huge":
        head.append(f"Content-Length: {DS.DRAIN_MAX + 1}")
    else:
        head.append("Transfer-Encoding: chunked")
    c = socket.create_connection(("127.0.0.1", s.port), timeout=10)
    try:
        c.sendall(("\r\n".join(head) + "\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = c.recv(4096)
            if not chunk:
                break
            buf += chunk
    finally:
        c.close()
    status, *lines = buf.decode("latin-1").split("\r\n")
    hdrs = {k.lower(): v for k, _, v in (x.partition(": ") for x in lines if ": " in x)}
    assert status.split()[1] == "401" and hdrs.get("connection") == "close"


def test_missing_host_is_403(s):
    c = http.client.HTTPConnection("127.0.0.1", s.port, timeout=10)
    c.putrequest("GET", "/api/snapshot", skip_host=True, skip_accept_encoding=True)
    c.putheader("Authorization", f"Bearer {s.token}")
    c.endheaders()
    assert c.getresponse().status == 403
    c.close()


def test_bound_and_localhost_hosts_pass(s):
    for host in (f"127.0.0.1:{s.port}", f"localhost:{s.port}"):
        st, _, _ = s.req(host=False, headers={"Host": host, "Origin": f"http://{host}"})
        assert st == 200
    assert s.audit() == []


@pytest.mark.parametrize("m", ["POST", "PUT", "DELETE", "OPTIONS", "PATCH"])
def test_other_methods_405_with_allow_get(s, m):
    st, h, _ = s.req(m, "/api/snapshot", body=b"{}")
    assert st == 405 and h["allow"] == "GET"
    st, h, _ = s.req(m, "/anything")                          # the connection still works
    assert st == 405
    assert s.audit()[-1]["reason"] == "method_not_allowed"


def test_head_is_405_audited_and_has_no_body(s):
    st, h, body = s.req("HEAD", "/api/snapshot")
    assert st == 405 and h["allow"] == "GET" and body == b""
    assert s.audit()[-1]["reason"] == "method_not_allowed"
    assert s.audit()[-1]["method"] == "HEAD"
    st, _, _ = s.req()                                        # the connection state is fine
    assert st == 200


def test_absurd_content_length_is_refused_not_a_handler_crash(s, capfd):
    import socket
    head = ["GET /api/snapshot HTTP/1.1", f"Host: 127.0.0.1:{s.port}",
            f"Authorization: Bearer {s.token}", "Content-Length: " + "1" * 5000]
    c = socket.create_connection(("127.0.0.1", s.port), timeout=10)
    try:
        c.sendall(("\r\n".join(head) + "\r\n\r\n").encode())
        buf = c.recv(4096)
    finally:
        c.close()
    assert buf.split(b" ")[1] == b"413"
    assert "Traceback" not in capfd.readouterr().err
    st, _, _ = s.req()
    assert st == 200


def test_404_and_413_and_503(tmp_path):
    fx = seed(tmp_path)

    class Flaky(SN.SnapshotCache):
        fail = False

        def get(self):
            if self.fail:
                raise RuntimeError("boom")
            return super().get()

    cache = Flaky(fx["cfg"])
    s = Srv(fx, cache)
    try:
        for p in ("/", "/nope", "/api/snapshot/x"):
            st, _, b = s.req(path=p)
            assert st == 404 and json.loads(b) == {"error": "not found"}
        st, h, _ = s.req(body=b"abc")
        assert st == 413 and h.get("connection") == "close"
        cache.fail = True
        st, _, b = s.req()
        assert st == 503 and json.loads(b)["error"] == "RuntimeError: boom"
        cache.fail = False
        assert s.req()[0] == 200
        assert s.audit() == []                                # none of these is a refusal
    finally:
        s.close()


def test_every_refusal_reason_is_audited_and_reads_are_not(s):
    s.req()
    s.req(headers={"If-None-Match": "*"})
    assert s.audit() == []
    s.req(host=False, headers={"Host": "evil"})
    s.req(headers={"Origin": "http://evil.example"})
    s.req(auth=False)
    s.req(auth=False, headers={"Authorization": "Bearer x"})
    s.req("POST")
    recs = s.audit()
    assert [(r["reason"], r["status"]) for r in recs] == [
        ("bad_host", 403), ("bad_origin", 403), ("no_token", 401), ("bad_token", 401),
        ("method_not_allowed", 405)]
    assert all(r["schema"] == "nw.dashboard.audit.v1" and r["path"] == "/api/snapshot"
               for r in recs)
    assert "Bearer" not in json.dumps(recs)


def test_idle_connection_is_closed_after_the_handler_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(DS, "HANDLER_TIMEOUT_S", 0.3)
    s = Srv(seed(tmp_path))
    try:
        import socket
        c = socket.create_connection(("127.0.0.1", s.port), timeout=10)
        assert c.recv(1) == b""                               # the server closed it
        c.close()
    finally:
        s.close()


@pytest.mark.parametrize("bind,word", [
    ("0.0.0.0:0", "wildcard"), ("[::]:0", "wildcard"), ("192.168.1.20:0", "loopback"),
    ("example.com:8791", "not an IP")])
def test_make_server_runs_the_bind_gate_itself(tmp_path, bind, word):
    """G2: a direct caller cannot skip the gate; nothing is bound or created."""
    import socket
    fx = seed(tmp_path)
    busy = socket.socket().connect_ex(("127.0.0.1", 8791)) == 0     # pre-existing listener, if any
    with pytest.raises(A.BindRefused, match=word):
        DS.make_server(fx["cfg"], bind)
    if not busy:
        probe = socket.socket()
        try:
            assert probe.connect_ex(("127.0.0.1", 8791)) != 0
        finally:
            probe.close()
    assert not (fx["data"] / "agentic/dashboard/server.json").exists()


# ---- SPEC-0403: the full decisions inbox over HTTP, with no server change ------------------

def test_snapshot_serves_full_inbox(tmp_path):
    from fixtures.dashboard_data import I_DEC, seed_inbox
    from nightwright.agentic import escalations as ESC
    fx = seed_inbox(tmp_path)
    srv = Srv(fx)
    try:
        st, h, body = srv.req()
        assert st == 200
        inbox = json.loads(body)["inbox"]
        want = json.loads(json.dumps(SN.build(srv.cfg)["inbox"]))
        assert SN._strip(inbox) == SN._strip(want)        # ages may tick between the builds
        assert "ESC-0001" in [e["id"] for e in inbox["escalations"]]
        assert inbox["escalations"][1]["findings"] and inbox["escalations"][1]["effects"]
        # the human answers ESC-0001 in the data dir (as `nw escalations answer` does)
        plan = ESC.plan_answer(srv.cfg, "ESC-0001", choose="1", by="human")
        assert ESC.apply_answer(srv.cfg, plan)["status"] == "answered"
        srv.clock.t += srv.cache.refresh_s
        st, h2, body = srv.req(headers={"If-None-Match": h["etag"]})
        assert st == 200 and h2["etag"] != h["etag"]
        inbox = json.loads(body)["inbox"]
        assert "ESC-0001" not in [e["id"] for e in inbox["escalations"]]
        parks = {p["campaign"]: p for p in inbox["parks"]}
        assert parks[I_DEC]["inbox_state"] == "awaiting_conductor"
    finally:
        srv.close()
