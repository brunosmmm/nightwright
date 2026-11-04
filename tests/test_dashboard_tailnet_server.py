"""SPEC-0416: `make_server` applies the tailnet bind gate itself (`own=` stubs, no tailnet)."""
import inspect

import pytest

from fixtures.dashboard_data import seed
from nightwright.agentic.dashboard import auth as A
from nightwright.agentic.dashboard import server as DS


def own_ok():
    return ["100.64.0.5"]


def own_missing():
    raise A.TailnetMissing("no tailnet address: run `tailscale up`")


@pytest.fixture
def no_socket(monkeypatch):
    """Creating the server object at all fails the test: a refused bind never gets that far."""
    def boom(*a, **k):
        raise AssertionError("a socket was created for a refused bind")
    monkeypatch.setattr(DS, "DashboardServer", boom)


def check_untouched(fx):
    assert not (fx["data"] / "agentic/dashboard/server.json").exists()


@pytest.mark.parametrize("bind,word", [
    ("0.0.0.0:0", "wildcard"), ("[::]:0", "wildcard"),
    ("192.168.1.20:0", "not a loopback or tailnet address"), ("8.8.8.8:0", "loopback or tailnet"),
    ("example.com:8791", "not an IP"),
    ("100.64.0.9:0", "foreign tailnet address"),
    ("100.64.0.5:0", "identity gate"), ("tailscale", "identity gate"),
    ("TAILSCALE:9000", "identity gate")])
def test_refused_binds_raise_bind_refused_before_a_socket(tmp_path, no_socket, bind, word):
    fx = seed(tmp_path)
    with pytest.raises(A.BindRefused, match=word):
        DS.make_server(fx["cfg"], bind, own=own_ok)
    check_untouched(fx)


@pytest.mark.parametrize("bind", ["tailscale", "100.64.0.9:0", "100.64.0.5:0"])
def test_no_tailnet_raises_tailnet_missing_before_a_socket(tmp_path, no_socket, bind):
    fx = seed(tmp_path)
    with pytest.raises(A.TailnetMissing, match="tailscale up"):
        DS.make_server(fx["cfg"], bind, own=own_missing)
    check_untouched(fx)


def test_loopback_serves_with_token_auth_and_never_asks_for_the_tailnet(tmp_path):
    fx = seed(tmp_path)
    calls = []
    srv = DS.make_server(fx["cfg"], "127.0.0.1:0", own=lambda: calls.append(1) or own_missing())
    try:
        rec = DS.write_server_json(fx["cfg"], srv)
        assert rec["auth"] == "token" and rec["host"] == "127.0.0.1"
        assert calls == []
    finally:
        DS.remove_server_json(fx["cfg"])
        srv.server_close()


def test_own_is_keyword_only():
    p = inspect.signature(DS.make_server).parameters["own"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is None
