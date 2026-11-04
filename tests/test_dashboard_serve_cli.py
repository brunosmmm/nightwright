"""SPEC-0414: `nw serve --dashboard` as a real subprocess against the seeded data dir."""
import http.client
import json
import os
import signal
import socket
import stat
import subprocess
import time
from pathlib import Path

import pytest

from fixtures.dashboard_data import seed
from test_dashboard_cli import nw_argv, nw_env
from waits import until


def dash(fx) -> Path:
    return fx["data"] / "agentic" / "dashboard"


def stub_env(fx, tmp_path):
    """`nw_env` plus a recording `tailscale` stub first on PATH."""
    env = nw_env(fx)
    bindir = tmp_path / "stubbin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "tailscale"
    stub.write_text(f'#!/bin/sh\necho "$@" >> {tmp_path}/tailscale.calls\nexit 1\n')
    stub.chmod(0o755)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    return env


def run_serve(fx, *args, env=None):
    return subprocess.run([*nw_argv(), "serve", *args], env=env or nw_env(fx),
                          capture_output=True, text=True, timeout=120)


def start(fx, *args, env=None):
    return subprocess.Popen([*nw_argv(), "serve", "--dashboard", *args],
                            env=env or nw_env(fx), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)


def get(port, token=None, path="/api/snapshot"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", path, headers={"Authorization": f"Bearer {token}"} if token else {})
    r = c.getresponse()
    return r.status, r.read()


def wait_server(fx, proc):
    p = dash(fx) / "server.json"
    assert until(lambda: p.exists() or proc.poll() is not None)
    assert proc.poll() is None, proc.stderr.read()
    return json.loads(p.read_text())


def stop(proc):
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
    try:
        return proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.communicate()


def listening(port):
    s = socket.socket()
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


@pytest.fixture
def fx(tmp_path):
    return seed(tmp_path)


@pytest.fixture
def tls_files(tmp_path):
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.write_text("dummy")
    key.write_text("dummy")
    return ["--tls-cert", str(cert), "--tls-key", str(key)]


REFUSED = {"0.0.0.0:0": "wildcard", "[::]:0": "wildcard", "::": "wildcard",
           "192.168.1.20:0": "loopback", "100.64.0.9:0": "loopback", "tailscale": "not an IP",
           "example.com:8791": "not an IP"}


@pytest.mark.parametrize("bind,word", list(REFUSED.items()))
@pytest.mark.parametrize("with_tls", [False, True])
def test_refused_binds_exit_2_with_the_rule(fx, tmp_path, tls_files, bind, word, with_tls):
    busy = bind.endswith(":8791") and listening(8791)
    r = run_serve(fx, "--dashboard", "--bind", bind, *(tls_files if with_tls else []),
                  env=stub_env(fx, tmp_path))
    assert r.returncode == 2, r.stderr
    assert "refusing to bind" in r.stderr and "TLS is not supported" not in r.stderr
    assert word in r.stderr
    assert not (dash(fx) / "server.json").exists()
    assert not (tmp_path / "tailscale.calls").exists()
    if bind.endswith(":8791") and not busy:
        assert not listening(8791)


def test_loopback_with_tls_flags_is_refused_with_the_tls_message(fx, tls_files):
    r = run_serve(fx, "--dashboard", "--bind", "127.0.0.1:0", *tls_files)
    assert r.returncode == 2
    assert "TLS is not supported with --dashboard" in r.stderr
    assert "refusing to bind" not in r.stderr
    assert not (dash(fx) / "server.json").exists()


def test_client_ca_alone_is_refused_with_the_tls_message(fx, tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("dummy")
    r = run_serve(fx, "--dashboard", "--bind", "127.0.0.1:0", "--client-ca", str(ca))
    assert r.returncode == 2
    assert "TLS is not supported with --dashboard" in r.stderr
    assert not (dash(fx) / "server.json").exists()


def test_port_in_use_exits_1(fx):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    try:
        r = run_serve(fx, "--dashboard", "--bind", f"127.0.0.1:{s.getsockname()[1]}")
    finally:
        s.close()
    assert r.returncode == 1 and "Usage:" not in r.stderr
    assert not (dash(fx) / "server.json").exists()


def test_exactly_one_mode_flag(fx):
    assert run_serve(fx).returncode == 2
    assert run_serve(fx, "--jobs", "--dashboard").returncode == 2


def test_jobs_keeps_its_default_bind(fx):
    out = run_serve(fx, "--help").stdout
    assert "--jobs" in out and "--dashboard" in out and "8790" in out


def test_jobs_alone_serves_on_its_default_bind(fx):
    """AC3: `nw serve --jobs` with no `--bind` really starts and listens on 127.0.0.1:8790."""
    if listening(8790):
        pytest.skip("127.0.0.1:8790 is busy")
    proc = subprocess.Popen([*nw_argv(), "serve", "--jobs"], env=nw_env(fx),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert until(lambda: listening(8790) or proc.poll() is not None)
        assert proc.poll() is None, proc.stderr.read()
        proc.send_signal(signal.SIGINT)            # `--jobs` stops on KeyboardInterrupt
        _, err = proc.communicate(timeout=30)
    finally:
        stop(proc)
    assert proc.returncode == 0, err
    assert "job API on http://127.0.0.1:8790" in err
    assert not (dash(fx) / "server.json").exists()     # nothing of the dashboard mode


def test_jobs_with_an_explicit_bind_uses_it(fx):
    proc = subprocess.Popen([*nw_argv(), "serve", "--jobs", "--bind", "127.0.0.1:0"],
                            env=nw_env(fx), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    try:
        line = proc.stderr.readline()
        assert proc.poll() is None, line
        assert line.startswith("nw serve: job API on http://127.0.0.1:")
        assert not line.rstrip().endswith(":8790")
        proc.send_signal(signal.SIGINT)
        proc.communicate(timeout=30)
    finally:
        stop(proc)
    assert proc.returncode == 0


def test_dashboard_without_bind_binds_8791(fx):
    s = socket.socket()
    if s.connect_ex(("127.0.0.1", 8791)) == 0:
        s.close()
        pytest.skip("127.0.0.1:8791 is busy")
    s.close()
    proc = start(fx)
    try:
        rec = wait_server(fx, proc)
        assert rec["port"] == 8791 and rec["host"] == "127.0.0.1"
    finally:
        stop(proc)


def test_serve_snapshot_token_sigterm_and_no_tailscale(fx, tmp_path):
    proc = start(fx, "--bind", "127.0.0.1:0", env=stub_env(fx, tmp_path))
    try:
        rec = wait_server(fx, proc)
        assert rec["auth"] == "token" and rec["pid"] == proc.pid and rec["port"] > 0
        d = dash(fx)
        tok = (d / "local.token").read_text().strip()
        assert stat.S_IMODE((d / "local.token").stat().st_mode) == 0o600
        assert stat.S_IMODE(d.stat().st_mode) == 0o700
        st, body = get(rec["port"], tok)
        doc = json.loads(body)
        assert st == 200 and doc["schema"] == "nw.dashboard.v1"
        assert get(rec["port"])[0] == 401
    finally:
        _, err = stop(proc)
    assert proc.returncode == 0
    assert not (d / "server.json").exists()
    assert tok not in err and tok not in (d / "audit.jsonl").read_text()
    assert not (tmp_path / "tailscale.calls").exists()


def test_token_is_reused_across_starts_and_not_in_server_json(fx):
    toks = []
    for _ in range(2):
        proc = start(fx, "--bind", "127.0.0.1:0")
        try:
            wait_server(fx, proc)
            toks.append((dash(fx) / "local.token").read_text().strip())
            assert toks[-1] not in (dash(fx) / "server.json").read_text()
        finally:
            stop(proc)
    assert toks[0] == toks[1]


def test_stale_server_json_is_replaced_and_foreign_one_is_kept(fx):
    d = dash(fx)
    d.mkdir(parents=True)
    sj = d / "server.json"
    sj.write_text(json.dumps({"pid": 999999, "port": 1}))
    proc = start(fx, "--bind", "127.0.0.1:0")
    try:
        assert until(lambda: sj.exists() and json.loads(sj.read_text()).get("pid") == proc.pid)
        assert proc.poll() is None
        # another process takes the record over: this server's shutdown must leave it alone
        sj.write_text(json.dumps({"pid": 1, "port": 2}))
    finally:
        stop(proc)
    assert proc.returncode == 0
    assert json.loads(sj.read_text()) == {"pid": 1, "port": 2}


def test_sigint_shuts_down_cleanly_and_server_json_is_0600(fx):
    proc = start(fx, "--bind", "127.0.0.1:0")
    sj = dash(fx) / "server.json"
    try:
        wait_server(fx, proc)
        assert stat.S_IMODE(sj.stat().st_mode) == 0o600
        proc.send_signal(signal.SIGINT)
        proc.communicate(timeout=30)
    finally:
        stop(proc)
    assert proc.returncode == 0
    assert not sj.exists()


def test_reference_docs_list_serve_dashboard():
    root = Path(__file__).resolve().parent.parent
    assert "serve --dashboard" in (root / "docs/product/reference/index.md").read_text()


def test_signal_handlers_are_installed_before_server_json_is_written(fx, monkeypatch):
    """A SIGTERM between `server.json` and the handler install would skip the cleanup."""
    from nightwright.agentic import cli_worker as CW
    from nightwright.agentic.dashboard import server as DS
    seen = {}

    class Stop(Exception):
        pass

    def fake_write(cfg, srv):
        seen["term"] = signal.getsignal(signal.SIGTERM)
        seen["int"] = signal.getsignal(signal.SIGINT)
        raise Stop

    monkeypatch.setattr(DS, "write_server_json", fake_write)
    old = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    try:
        with pytest.raises(Stop):
            CW._serve_dashboard(fx["cfg"], "127.0.0.1:0", None, None, None)
    finally:
        signal.signal(signal.SIGTERM, old[0])
        signal.signal(signal.SIGINT, old[1])
    assert seen["term"] is not old[0] and callable(seen["term"])
    assert seen["int"] is not old[1] and callable(seen["int"])
    assert not (dash(fx) / "server.json").exists()
