"""SPEC-0416: the tailnet bind gate through `nw serve --dashboard` as a real subprocess, with the
fake `tailscale` (no tailnet needed)."""
import json
import os
import signal
import subprocess

import pytest

from fixtures import fake_tailscale as FT
from fixtures.dashboard_data import seed
from test_dashboard_cli import nw_env
from test_dashboard_serve_cli import dash, listening, run_serve, start, stop, wait_server
from waits import until


@pytest.fixture
def fx(tmp_path):
    return seed(tmp_path)


@pytest.fixture
def tls_files(tmp_path):
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.write_text("dummy")
    key.write_text("dummy")
    return ["--tls-cert", str(cert), "--tls-key", str(key)]


def fake_env(fx, tmp_path, scenario=None, mode="ok"):
    return FT.install(tmp_path, scenario, mode, base_env=nw_env(fx))


def no_tailscale_env(fx):
    """`nw_env` already drops every PATH entry holding a `tailscale`: the binary is missing."""
    return nw_env(fx)


def untouched(fx):
    assert not (dash(fx) / "server.json").exists()
    assert not listening(8791)


FAILURES = [("logged_out", {"logged_out": True}, "ok"), ("fail", None, "fail"),
            ("garbage", None, "garbage"), ("hang", None, "hang")]


@pytest.mark.parametrize("bind", ["tailscale", "100.64.0.9:0"])
@pytest.mark.parametrize("name,scenario,mode", FAILURES, ids=[f[0] for f in FAILURES])
def test_no_tailnet_exits_3_naming_tailscale_up(fx, tmp_path, name, scenario, mode, bind):
    env = fake_env(fx, tmp_path, scenario, mode)
    r = run_serve(fx, "--dashboard", "--bind", bind, env=env)
    assert r.returncode == 3, r.stderr
    assert "tailscale up" in r.stderr and "Traceback" not in r.stderr
    assert FT.calls(env) == [["ip"]]
    untouched(fx)


@pytest.mark.parametrize("bind", ["tailscale", "100.64.0.9:0"])
def test_no_tailnet_with_tls_flags_still_exits_3(fx, tmp_path, tls_files, bind):
    env = fake_env(fx, tmp_path, {"logged_out": True})
    r = run_serve(fx, "--dashboard", "--bind", bind, *tls_files, env=env)
    assert r.returncode == 3, r.stderr
    assert "tailscale up" in r.stderr and "TLS is not supported" not in r.stderr
    untouched(fx)


@pytest.mark.parametrize("bind", ["tailscale", "100.64.0.9:0"])
@pytest.mark.parametrize("with_tls", [False, True])
def test_tailscale_missing_from_path_exits_3(fx, tmp_path, tls_files, bind, with_tls):
    r = run_serve(fx, "--dashboard", "--bind", bind, *(tls_files if with_tls else []),
                  env=no_tailscale_env(fx))
    assert r.returncode == 3, r.stderr
    assert "tailscale up" in r.stderr
    untouched(fx)


REFUSED = {"0.0.0.0:0": "wildcard", "[::]:0": "wildcard", "192.168.1.20:0": "loopback or tailnet",
           "8.8.8.8:0": "loopback or tailnet", "example.com:8791": "not an IP",
           "100.64.0.9:0": "foreign tailnet address"}


@pytest.mark.parametrize("bind,word", list(REFUSED.items()))
@pytest.mark.parametrize("with_tls", [False, True])
def test_refused_binds_exit_2_with_the_rule(fx, tmp_path, tls_files, bind, word, with_tls):
    env = fake_env(fx, tmp_path)                    # own address 100.64.0.5
    r = run_serve(fx, "--dashboard", "--bind", bind, *(tls_files if with_tls else []), env=env)
    assert r.returncode == 2, r.stderr
    assert "refusing to bind" in r.stderr and word in r.stderr
    assert "TLS is not supported" not in r.stderr
    # only a tailnet-range address may ask the tailnet; the others never run `tailscale`
    assert FT.calls(env) == ([["ip"]] if bind.startswith("100.") else [])
    untouched(fx)


@pytest.mark.parametrize("bind", ["tailscale", "TAILSCALE:0", "100.64.0.5:0"])
@pytest.mark.parametrize("with_tls", [False, True])
def test_own_tailnet_address_is_refused_by_the_identity_gate(fx, tmp_path, tls_files, bind,
                                                              with_tls):
    env = fake_env(fx, tmp_path)
    r = run_serve(fx, "--dashboard", "--bind", bind, *(tls_files if with_tls else []), env=env)
    assert r.returncode == 2, r.stderr
    assert "identity gate" in r.stderr and "100.64.0.5" in r.stderr
    assert "TLS is not supported" not in r.stderr
    assert FT.calls(env) == [["ip"]]
    untouched(fx)


def test_default_bind_is_still_loopback_and_never_runs_tailscale(fx, tmp_path):
    env = fake_env(fx, tmp_path)
    p = start(fx, env=env)
    try:
        rec = wait_server(fx, p)
        assert rec["auth"] == "token" and rec["host"] == "127.0.0.1" and rec["port"] == 8791
        assert FT.calls(env) == []
    finally:
        stop(p)


def test_explicit_loopback_serves_with_token_and_never_runs_tailscale(fx, tmp_path):
    env = fake_env(fx, tmp_path)
    p = start(fx, "--bind", "127.0.0.1:0", env=env)
    try:
        rec = wait_server(fx, p)
        assert rec["auth"] == "token" and rec["host"] == "127.0.0.1"
        assert FT.calls(env) == []
    finally:
        stop(p)
    assert not (dash(fx) / "server.json").exists()


def test_loopback_with_tls_flags_still_exits_2_with_the_tls_message(fx, tmp_path, tls_files):
    env = fake_env(fx, tmp_path)
    r = run_serve(fx, "--dashboard", "--bind", "127.0.0.1:0", *tls_files, env=env)
    assert r.returncode == 2 and "TLS is not supported with --dashboard" in r.stderr
    assert FT.calls(env) == []


def test_only_tailscale_ip_is_ever_run(fx, tmp_path):
    env = fake_env(fx, tmp_path)
    for bind in ("tailscale", "100.64.0.5:0", "100.64.0.9:0"):
        run_serve(fx, "--dashboard", "--bind", bind, env=env)
    assert FT.calls(env) and all(c == ["ip"] for c in FT.calls(env))
