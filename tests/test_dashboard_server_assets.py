"""SPEC-0418: the dashboard server serves the phone page (`GET /`, `/app.js`, `/app.css`) through
`assets.read_asset`, without a bearer, behind the `Host`/`Origin` check. Real loopback server over
real sockets (the `Srv` helper of test_dashboard_server), no browser, no `tailscale`."""
import json
import re
from pathlib import Path

import pytest

from fixtures.dashboard_data import seed
from test_dashboard_page import Stub, get as stub_get
from test_dashboard_readonly import tree
from test_dashboard_serve_cli import get as cli_get, start, stop, wait_server
from test_dashboard_server import Srv
from nightwright.agentic.dashboard import assets
from nightwright.agentic.dashboard import auth as A
from nightwright.agentic.dashboard import server as DS

ROOT = Path(__file__).resolve().parent.parent
ALLOWED_DASH_FILES = {"local.token", "audit.jsonl", "audit.jsonl.1", "server.json"}


@pytest.fixture
def s(tmp_path):
    srv = Srv(seed(tmp_path))
    yield srv
    srv.close()


def asset_bytes(path):
    return assets.read_asset(path)[0]


def test_static_routes_unauthenticated_200(s):
    cases = [(p, p) for p in assets.ROUTES] + [("/app.js?v=1", "/app.js"), ("/?x=1", "/")]
    for req_path, route in cases:
        body, ctype = assets.read_asset(route)
        for kw in ({"auth": False}, {"auth": True},
                   {"auth": False, "headers": {"Authorization": "Bearer wrong"}}):
            st, h, got = s.req(path=req_path, **kw)
            assert st == 200, (req_path, kw)
            assert got == body
            assert h["content-type"] == ctype
            assert h["content-security-policy"] == assets.CSP
            assert h["cache-control"] == "no-cache"
            assert h["x-content-type-options"] == "nosniff"
            assert h["content-length"] == str(len(body))
            assert "date" in h
    assert s.audit() == []                       # a 200 static response is not audited


def test_header_parity_with_page_stub(s, tmp_path):
    """Exact `ROUTES` paths only; the stub does not strip query strings, so its 404 for
    `/app.js?x=1` is a stub artifact; the server's contract is the query-dropping of Decision 1."""
    with Stub(seed(tmp_path / "stub")) as stub:
        for path in assets.ROUTES:
            _, sh, sbody = stub_get(stub, path)
            st, h, body = s.req(path=path, auth=False)
            assert st == 200 and body == sbody
            for name in ("content-type", "content-security-policy", "x-content-type-options",
                         "cache-control"):
                assert h[name] == {k.lower(): v for k, v in sh.items()}[name], (path, name)


def test_api_snapshot_still_gated(s):
    st, h, b = s.req(auth=False)
    assert st == 401 and h["www-authenticate"] == "Bearer"
    assert json.loads(b) == {"error": "unauthorized"}
    st, h, _ = s.req(auth=False, headers={"Authorization": "Bearer nope"})
    assert st == 401 and h["www-authenticate"] == "Bearer"
    assert [r["reason"] for r in s.audit()] == ["no_token", "bad_token"]
    st, h, _ = s.req()
    assert st == 200 and h["etag"]
    st, h2, b = s.req(headers={"If-None-Match": h["etag"]})
    assert st == 304 and b == b""


# `//app.js`: http.server folds the leading `//` into `/` before the handler runs; the server
# still treats the request as sent (not a route)
NON_ROUTES = ["/nope", "/index.html", "/static/app.js", "/../app.js", "//app.js", "/%61pp.js",
              "/app.js/"]


def test_non_route_paths_stay_401_then_404(s):
    served = [asset_bytes(p) for p in assets.ROUTES]
    for p in NON_ROUTES:
        st, _, b = s.req(path=p, auth=False)
        assert st == 401, p
        assert not any(a in b for a in served)
        st, _, b = s.req(path=p)
        assert st == 404 and json.loads(b) == {"error": "not found"}, p
        assert not any(a in b for a in served)


def test_static_behind_host_and_origin(s, monkeypatch):
    served = [asset_bytes(p) for p in assets.ROUTES]
    port = s.port
    cases = [
        ({"host": False}, "bad_host"),
        ({"headers": {"Host": "evil.example"}}, "bad_host"),
        ({"headers": {"Host": f"127.0.0.1:{port + 1}"}}, "bad_host"),
        ({"headers": {"Origin": "http://evil"}}, "bad_origin"),
        ({"headers": {"Origin": "null"}}, "bad_origin"),
    ]
    for i, (kw, reason) in enumerate(cases, 1):
        st, _, b = s.req(path="/", auth=False, **kw)
        assert st == 403 and "error" in json.loads(b), kw
        assert not any(a in b for a in served)
        log = s.audit()
        assert len(log) == i and log[-1]["reason"] == reason, kw
    st, _, b = s.req(path="/app.js", auth=False, headers={"Host": f"localhost:{port}"})
    assert st == 200 and b == asset_bytes("/app.js")
    assert len(s.audit()) == len(cases)
    monkeypatch.setattr(A, "check_host_origin", lambda headers, allowed: "bad_host")
    st, _, b = s.req(path="/", auth=False)
    assert st == 403 and not any(a in b for a in served)


def test_static_methods_and_body(s):
    served = [asset_bytes(p) for p in assets.ROUTES]
    n = 0
    for m in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"):
        st, h, b = s.req(method=m, path="/", auth=False)
        assert st == 405 and h["allow"] == "GET", m
        assert not any(a in b for a in served)
        n += 1
        log = s.audit()
        assert len(log) == n and log[-1]["reason"] == "method_not_allowed", m
    st, h, b = s.req(path="/", auth=False, body=b"abc")
    assert st == 413 and h.get("connection") == "close"
    assert not any(a in b for a in served)
    assert len(s.audit()) == n + 1
    st, _, _ = s.req(path="/", auth=False)
    assert st == 200 and len(s.audit()) == n + 1


def test_read_asset_failure_is_500(s, monkeypatch):
    real = assets.read_asset

    def fake(path):
        if path in assets.ROUTES:
            raise OSError("packaged file missing")
        return real(path)

    monkeypatch.setattr(DS.AS, "read_asset", fake)
    st, _, b = s.req(path="/", auth=False)
    assert st == 500
    assert re.fullmatch(r"OSError: .*", json.loads(b)["error"])
    st, _, b = s.req()                           # patch still active
    assert st == 200 and json.loads(b)["schema"] == "nw.dashboard.v1"


def test_serving_assets_writes_nothing(tmp_path):
    fx = seed(tmp_path)
    s = Srv(fx)
    try:
        before = tree(fx["data"])
        for p in assets.ROUTES:
            assert s.req(path=p, auth=False)[0] == 200
        after = tree(fx["data"])
        assert after == before
        dash = fx["data"] / "agentic" / "dashboard"
        assert {p.name for p in dash.iterdir()} <= ALLOWED_DASH_FILES
    finally:
        s.close()
    with pytest.raises(A.BindRefused):
        DS.make_server(fx["cfg"], "0.0.0.0:0")


def test_serve_cli_serves_page(tmp_path):
    fx = seed(tmp_path)
    proc = start(fx, "--bind", "127.0.0.1:0")
    try:
        rec = wait_server(fx, proc)
        st, body = cli_get(rec["port"], path="/")
        index = (ROOT / "src/nightwright/agentic/dashboard/static/index.html").read_bytes()
        assert st == 200 and body == index
        assert cli_get(rec["port"])[0] == 401
    finally:
        stop(proc)


def test_spec_0413_depends_on_includes_0414():
    text = next((ROOT / "docs/specs").glob("0413-*.md")).read_text()
    fm = text.split("---")[1]
    m = re.search(r"^depends_on:\s*\[(.*?)\]", fm, re.M)
    assert [x.strip() for x in m.group(1).split(",")] == ["SPEC-0401", "SPEC-0414"]
