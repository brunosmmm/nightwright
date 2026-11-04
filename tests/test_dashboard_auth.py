"""SPEC-0414: dashboard bind gate, local token and Host/Origin check (unit, no sockets)."""
import os
import stat

import pytest

from nightwright.agentic.dashboard import auth as A


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
    ("192.168.1.20", "loopback"), ("100.64.0.9", "loopback"), ("10.0.0.1", "loopback"),
    ("::ffff:192.168.1.20", "loopback"), ("fd7a:115c:a1e0::1", "loopback"),
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


def mode(p):
    return stat.S_IMODE(p.stat().st_mode)


def test_local_token_created_0600_in_0700_dir_and_reused(tmp_path):
    cfg = {"data_dir": str(tmp_path)}
    tok = A.local_token(cfg)
    d = tmp_path / "agentic" / "dashboard"
    assert len(tok) >= 32 and mode(d) == 0o700 and mode(d / "local.token") == 0o600
    assert A.local_token(cfg) == tok


def test_local_token_repairs_mode_and_regenerates_empty(tmp_path):
    cfg = {"data_dir": str(tmp_path)}
    tok = A.local_token(cfg)
    p = tmp_path / "agentic" / "dashboard" / "local.token"
    os.chmod(p, 0o644)
    assert A.local_token(cfg) == tok and mode(p) == 0o600
    p.write_text("")
    os.chmod(p, 0o644)
    new = A.local_token(cfg)
    assert new and new != tok and mode(p) == 0o600 and p.read_text().strip() == new


def test_local_token_non_utf8_file_is_regenerated(tmp_path):
    cfg = {"data_dir": str(tmp_path)}
    A.local_token(cfg)
    p = tmp_path / "agentic" / "dashboard" / "local.token"
    p.write_bytes(b"\xff\xfe\x80garbage")
    new = A.local_token(cfg)
    assert new and p.read_text().strip() == new and mode(p) == 0o600


def test_local_token_concurrent_first_starts_agree(tmp_path):
    import threading
    cfg = {"data_dir": str(tmp_path)}
    out, start = [], threading.Barrier(8)

    def go():
        start.wait()
        out.append(A.local_token(cfg))
    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    d = tmp_path / "agentic" / "dashboard"
    assert len(out) == 8 and len(set(out)) == 1
    assert (d / "local.token").read_text().strip() == out[0]
    assert [x.name for x in d.iterdir()] == ["local.token"]       # no temp file left behind


def test_host_values_brackets_ipv6():
    assert A.host_values("127.0.0.1", 8791) == {"127.0.0.1:8791", "localhost:8791"}
    assert A.host_values("::1", 8791) == {"[::1]:8791", "localhost:8791"}


@pytest.mark.parametrize("headers,want", [
    ({"Host": "127.0.0.1:8791"}, None),
    ({"Host": "localhost:8791"}, None),
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
