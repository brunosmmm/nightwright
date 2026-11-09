"""SPEC-0428: no credential on loopback, the Host allowlist, no CORS, and the per-page CSRF
value (`auth.Csrf`, embedded in `/` by the server)."""
import http.client
import re
import threading

import pytest

import waits
from fixtures.dashboard_data import seed
from nightwright.agentic.dashboard import assets as AS
from nightwright.agentic.dashboard import auth as A
from nightwright.agentic.dashboard import server as DS

pytestmark = pytest.mark.fast


def test_csrf_mint_and_check():
    c = A.Csrf(b"k" * 32)
    v = c.mint("local")
    assert c.check(v, "local") and re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", v)
    assert c.mint("local") != v                               # a fresh nonce per page
    assert not c.check(v, "me@x|100.64.0.2")                  # bound to the identity
    assert not A.Csrf(b"j" * 32).check(v, "local")            # bound to the process secret
    for bad in (None, "", ".", "a.b", v + "x", "x" + v, v.replace(".", ""), "a.b.c", "é.é",
                "a" * 300):
        assert not c.check(bad, "local"), bad
    assert c.verify(c.sign("p", "q"), "p", "q") and not c.verify(c.sign("p", "q"), "p", "r")


def test_render_index_fills_the_placeholder_once_and_refuses_odd_values():
    page = AS.read_asset("/")[0]
    assert page.count(AS.CSRF_PLACEHOLDER) == 1
    out = AS.render_index(page, "abc.def")
    assert b'<meta name="nw-csrf" content="abc.def">' in out and AS.CSRF_PLACEHOLDER not in out
    with pytest.raises(ValueError):
        AS.render_index(page, '"><script>')


def test_host_values_and_same_origin():
    hv = A.host_values("127.0.0.1", 8791)
    assert hv == {"127.0.0.1:8791", "localhost:8791", "[::1]:8791"}
    assert A.host_values("::1", 9, "local") == {"[::1]:9", "127.0.0.1:9", "localhost:9"}
    tv = A.host_values("100.64.0.5", 8791, "whois", "Box.tail1.ts.net.")
    assert tv == {"100.64.0.5:8791", "box.tail1.ts.net:8791", "box:8791"}
    ok = {"127.0.0.1:1"}
    assert A.check_same_origin({"Origin": "http://127.0.0.1:1"}, ok) is None
    assert A.check_same_origin({"Origin": "http://evil:1"}, ok) == "bad_origin"
    assert A.check_same_origin({"Origin": "null"}, ok) == "bad_origin"
    assert A.check_same_origin({}, ok) == "no_origin"
    assert A.check_same_origin({"Referer": "http://127.0.0.1:1/?x#y"}, ok) is None
    assert A.check_same_origin({"Referer": "http://127.0.0.1:1.evil/"}, ok) == "bad_origin"
    assert A.check_same_origin({"Referer": "https://127.0.0.1:1/"}, ok) == "bad_origin"
    assert A.bind_mode("127.0.0.1") == "local" and A.bind_mode("100.64.0.5") == "whois"


def test_resolve_bind_serves_own_tailnet_addresses_and_still_refuses_the_rest():
    own = lambda: ["100.64.0.5"]                              # noqa: E731
    assert A.resolve_bind("tailscale:0", own_fn=own) == ("100.64.0.5", 0)
    assert A.resolve_bind("100.64.0.5:9", own_fn=own) == ("100.64.0.5", 9)
    for bad in ("0.0.0.0:1", "[::]:1", "192.168.1.5:1", "100.64.0.9:1"):
        with pytest.raises(A.BindRefused):
            A.resolve_bind(bad, own_fn=own)


@pytest.fixture
def srv(tmp_path):
    fx = seed(tmp_path)
    s = DS.make_server(fx["cfg"], "127.0.0.1:0")
    t = threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield s, fx
    s.shutdown()
    waits.join(t, 10)
    s.server_close()


def get(port, path, headers=None, method="GET"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        c.request(method, path, headers={"Host": f"127.0.0.1:{port}", **(headers or {})})
        r = c.getresponse()
        return r.status, dict((k.lower(), v) for k, v in r.getheaders()), r.read()
    finally:
        c.close()


def test_loopback_needs_no_credential_and_makes_no_token(srv):
    s, fx = srv
    port = s.server_address[1]
    assert s.mode == "local" and not hasattr(s, "token")
    for path in ("/", "/app.js", "/app.css", "/api/snapshot"):
        assert get(port, path)[0] == 200, path
        assert get(port, path, {"Authorization": "Bearer junk"})[0] == 200, path
    for host in (f"localhost:{port}", f"[::1]:{port}"):
        assert get(port, "/api/snapshot", {"Host": host})[0] == 200
    assert not (fx["data"] / "agentic" / "dashboard" / "local.token").exists()


def test_each_page_load_gets_a_fresh_valid_csrf_value(srv):
    s, _ = srv
    port = s.server_address[1]
    vals = []
    for _ in range(2):
        st, h, body = get(port, "/")
        assert st == 200 and h["cache-control"] == "no-store"
        vals.append(re.search(rb'name="nw-csrf" content="([^"]+)"', body).group(1).decode())
    assert vals[0] != vals[1] and all(s.csrf.check(v, "local") for v in vals)


def test_foreign_hosts_are_refused_and_no_response_has_cors_headers(srv):
    s, _ = srv
    port = s.server_address[1]
    for host in ("evil.example:%d" % port, "127.0.0.1.nip.io:%d" % port, "127.0.0.1:1"):
        assert get(port, "/", {"Host": host})[0] == 403
    for method, path in (("GET", "/"), ("GET", "/api/snapshot"), ("OPTIONS", "/api/snapshot"),
                         ("GET", "/missing"), ("POST", "/api/escalations/ESC-0001/plan"),
                         ("GET", "/api/snapshot")):
        st, h, _ = get(port, path, method=method,
                       headers={"Origin": "http://evil.example"} if path == "/missing" else None)
        assert not [k for k in h if k.startswith("access-control-")], (method, path, h)
        if method == "OPTIONS":
            assert st == 405

