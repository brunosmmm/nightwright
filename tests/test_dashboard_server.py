"""SPEC-0414: the dashboard server in a thread on 127.0.0.1:0 over real sockets, against the
seeded data dir."""
import http.client
import json
import threading

import pytest

from fixtures.dashboard_data import ACTIVE, seed
from test_dashboard_snapshot import check_schema, ledger_run_end
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
