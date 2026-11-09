"""SPEC-0414/0428: dashboard bind gate and Host/Origin allowlist (unit, no sockets)."""

import pytest

from nightwright.agentic.dashboard import auth as A

pytestmark = pytest.mark.fast


@pytest.mark.parametrize("spec,want", [
    ("127.0.0.1:0", ("127.0.0.1", 0)),
    ("[::1]:8791", ("::1", 8791)),
    ("localhost:8791", ("127.0.0.1", 8791)),
    ("localhost", ("127.0.0.1", 8791)),
    ("127.0.0.1", ("127.0.0.1", 8791)),
    ("::1", ("::1", 8791)),
    ("[::]", ("::", 8791)),
    ("tailscale", ("tailscale", 8791)),
    ("tailscale:9000", ("tailscale", 9000)),
    ("example.com", ("example.com", 8791)),
])
def test_parse_bind(spec, want):
    assert A.parse_bind(spec) == want


@pytest.mark.parametrize("spec", ["", "   ", ":8791", "a:b:c", "127.0.0.1:abc", "127.0.0.1:",
                                  "127.0.0.1:65536", "[::1", "[::1]x", "[::1]:-1", None])
def test_parse_bind_garbage(spec):
    with pytest.raises(A.BindRefused):
        A.parse_bind(spec)


@pytest.mark.parametrize("host,word", [
    ("127.0.0.1", None), ("127.5.5.5", None), ("::1", None), ("::ffff:127.0.0.1", None),
    ("0.0.0.0", "wildcard"), ("::", "wildcard"), ("::ffff:0.0.0.0", "wildcard"),
    ("192.168.1.20", "loopback"), ("100.64.0.9", "foreign tailnet address"),
    ("10.0.0.1", "loopback"), ("::ffff:192.168.1.20", "loopback"),
    ("fd7a:115c:a1e0::1", "foreign tailnet address"),
    ("tailscale", "not an IP"), ("example.com", "not an IP"),
])
def test_check_bind(host, word):
    why = A.check_bind(host)
    if word is None:
        assert why is None
    else:
        assert why and word in why


@pytest.mark.parametrize("spec,word", [("0.0.0.0:0", "wildcard"), ("[::]:0", "wildcard"),
                                       ("192.168.1.20:0", "loopback"),
                                       ("100.64.0.9:0", "loopback"),
                                       ("tailscale", "not an IP"),
                                       ("example.com:8791", "not an IP")])
def test_validate_bind_refuses_with_the_rule(spec, word):
    with pytest.raises(A.BindRefused, match=word):
        A.validate_bind(spec)
    assert A.validate_bind("127.0.0.1:0") == ("127.0.0.1", 0)


def test_bind_refused_is_a_value_error():
    assert issubclass(A.BindRefused, ValueError)


def test_host_values_brackets_ipv6():
    loop = {"127.0.0.1:8791", "localhost:8791", "[::1]:8791"}
    assert A.host_values("127.0.0.1", 8791) == loop
    assert A.host_values("::1", 8791) == loop
    assert A.host_values("127.0.0.2", 8791) == loop | {"127.0.0.2:8791"}


def test_host_values_tailnet_bind_adds_magicdns_name_and_label_only():
    want = {"100.64.0.5:8791", "box.tail1234.ts.net:8791", "box:8791"}
    assert A.host_values("100.64.0.5", 8791, "whois", "box.tail1234.ts.net.") == want
    assert A.host_values("100.64.0.5", 8791, "whois") == {"100.64.0.5:8791"}


@pytest.mark.parametrize("headers,want", [
    ({"Host": "127.0.0.1:8791"}, None),
    ({"Host": "localhost:8791"}, None),
    ({"Host": "[::1]:8791"}, None),
    ({"Host": "[::1]:8791", "Origin": "http://[::1]:8791"}, None),
    ({"Host": "LOCALHOST:8791", "Origin": "http://localhost:8791"}, None),
    ({"Host": "127.0.0.1:8791", "Origin": "http://127.0.0.1:8791"}, None),
    ({"Host": "evil.example"}, "bad_host"),
    ({"Host": "127.0.0.1:1"}, "bad_host"),
    ({"Host": "127.0.0.1"}, "bad_host"),
    ({}, "bad_host"),
    ({"Host": "127.0.0.1:8791", "Origin": "http://evil.example"}, "bad_origin"),
    ({"Host": "127.0.0.1:8791", "Origin": "https://127.0.0.1:8791"}, "bad_origin"),
    ({"Host": "127.0.0.1:8791", "Origin": "null"}, "bad_origin"),
])
def test_check_host_origin(headers, want):
    assert A.check_host_origin(headers, A.host_values("127.0.0.1", 8791)) == want


def test_check_host_origin_bracketed_ipv6():
    allowed = A.host_values("::1", 8791)
    assert A.check_host_origin({"Host": "[::1]:8791", "Origin": "http://[::1]:8791"},
                               allowed) is None
    assert A.check_host_origin({"Host": "::1:8791"}, allowed) == "bad_host"

