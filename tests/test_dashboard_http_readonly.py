"""SPEC-0414 / SPEC-0400 invariant 1 over HTTP: the running server writes nothing outside
`agentic/dashboard/`, and only the four allowed files appear inside it."""
import http.client
import json

from fixtures.dashboard_data import ACTIVE, seed
from test_dashboard_readonly import tree
from test_dashboard_serve_cli import dash, get, start, stop, wait_server
from test_dashboard_snapshot import ledger_run_end
from waits import until

ALLOWED = {"local.token", "audit.jsonl", "audit.jsonl.1", "server.json"}


def outside(data):
    return {k: v for k, v in tree(data).items()
            if k != "agentic/dashboard" and not k.startswith("agentic/dashboard/")}


def raw(port, method, path, headers):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path, headers=headers)
    st = c.getresponse().status
    c.close()
    return st


def test_server_writes_nothing_outside_its_directory(tmp_path):
    fx = seed(tmp_path)
    data = fx["data"]
    proc = start(fx, "--bind", "127.0.0.1:0")
    try:
        rec = wait_server(fx, proc)
        port = rec["port"]
        tok = (dash(fx) / "local.token").read_text().strip()
        auth = {"Authorization": f"Bearer {tok}"}
        before = outside(data)

        def traffic():
            etag = None
            for i in range(100):
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                h = dict(auth)
                if etag and i % 2:
                    h["If-None-Match"] = etag
                c.request("GET", "/api/snapshot", headers=h)
                r = c.getresponse()
                r.read()
                etag = r.getheader("ETag")
                c.close()
            assert get(port)[0] == 401                               # no_token
            assert get(port, "wrong")[0] == 401                      # bad_token
            assert raw(port, "GET", "/api/snapshot", {**auth, "Origin": "http://evil.example"}) == 403
            assert raw(port, "GET", "/api/snapshot", {**auth, "Host": "evil.example"}) == 403
            assert raw(port, "POST", "/api/snapshot", auth) == 405

        traffic()
        assert outside(data) == before
        ledger_run_end(fx["cfg"], ACTIVE, 0.9)                       # a queue-side write
        before = outside(data)
        # the snapshot rebuilds on the next read after refresh_s; keep reading until it has
        first = json.loads(get(port, tok)[1])["rev"]
        assert until(lambda: json.loads(get(port, tok)[1])["rev"] != first, timeout=30)
        traffic()
        assert outside(data) == before
        assert {p.name for p in dash(fx).iterdir()} <= ALLOWED
        reasons = {json.loads(x)["reason"]
                   for x in (dash(fx) / "audit.jsonl").read_text().splitlines()}
        assert reasons == {"no_token", "bad_token", "bad_origin", "bad_host",
                           "method_not_allowed"}
    finally:
        stop(proc)
    assert {p.name for p in dash(fx).iterdir()} <= ALLOWED - {"server.json"}
