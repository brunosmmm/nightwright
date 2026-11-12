"""SPEC-0529: federation enrolment and the signed host channel."""
from __future__ import annotations

import ast
import http.client
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

from nightwright.agentic import cli_hosts
from nightwright.agentic import federation as F
from nightwright.agentic import federation_host as FH
from nightwright.agentic import hosts as H
from nightwright.agentic import pull_jobs as PJ
from nightwright.agentic import queue as Q
from nightwright.agentic import store as S
from nightwright.cli import cli

from test_agentic_dashboard import _campaign
from test_agentic_e2e import make_cfg, make_repo, script

pytestmark = pytest.mark.fast

NOW = 1_700_000_000.0


def _dirs(tmp_path, name="hub"):
    data = tmp_path / f"data-{name}"
    cfgd = tmp_path / f"cfg-{name}"
    data.mkdir()
    cfgd.mkdir()
    return data, cfgd


def _cfg(tmp_path, name="hub"):
    data, cfgd = _dirs(tmp_path, name)
    return {"data_dir": str(data), "config_dir": str(cfgd), "agentic": {}}


def _invoke(cfg, monkeypatch, *args, input=None):
    monkeypatch.setenv("NW_DATA_DIR", cfg["data_dir"])
    monkeypatch.setenv("NW_CONFIG_DIR", cfg["config_dir"])
    return CliRunner().invoke(cli, ["hosts", *args], input=input)


def _add_and_enrol(host_cfg, hub_cfg, monkeypatch, name="box", url="http://127.0.0.1:8792"):
    r = _invoke(hub_cfg, monkeypatch, "add", name, "--url", url)
    assert r.exit_code == 0, r.output
    blob = json.loads(r.output.strip())
    r2 = CliRunner().invoke(cli, ["hosts", "enrol"], input=json.dumps(blob) + "\n",
                            env={**os.environ, "NW_DATA_DIR": host_cfg["data_dir"],
                                 "NW_CONFIG_DIR": host_cfg["config_dir"]})
    assert r2.exit_code == 0, r2.output
    return blob


def _point_hub(hub_cfg, name, url):
    H.update(hub_cfg, lambda rs: H.find(rs, name).__setitem__("url", url))


def _serve(cfg):
    srv = FH.make_federate_server(cfg, "127.0.0.1:0")
    th = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    th.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _raw(method, url, path, token, secret, body=b"", *, now=NOW, headers=None):
    host, port = url.split("://")[1].split(":")
    port = int(port)
    conn = http.client.HTTPConnection(host, port, timeout=5)
    hdrs = headers if headers is not None else {
        "Authorization": f"Bearer {token}",
        **F.request_headers(secret, method, path, body, now=now),
    }
    conn.request(method, path, body=body, headers=hdrs)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, dict(resp.getheaders()), raw


def _resp_sig(secret, method, path, hdrs, raw, status):
    ts = hdrs.get("X-NW-Sig-Ts") or hdrs.get("x-nw-sig-ts")
    mac = hdrs.get("X-NW-Sig") or hdrs.get("x-nw-sig")
    assert ts and mac
    assert (hdrs.get(F.SIG_V_HEADER) or hdrs.get(F.SIG_V_HEADER.lower())) == F.SIG_VERSION
    assert F.verify(secret, method, path, raw, mac, int(ts), direction="resp", status=status,
                    now=NOW) is None


def _lease_checker_threads():
    for t in threading.enumerate():
        fn = getattr(getattr(t, "_target", None), "__func__", None)
        if fn is PJ.PullServer.check_leases:
            yield t


def _mini_tls_pair(tmp_path):
    if shutil.which("openssl") is None:
        pytest.skip("openssl is not installed")
    d = tmp_path / "tls"
    d.mkdir()
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "k.pem",
         "-out", "c.pem", "-days", "1", "-subj", "/CN=localhost",
         "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
        cwd=d, check=True, capture_output=True,
    )
    return str(d / "c.pem"), str(d / "k.pem")


def _queue_import_violations(source: str, *, federation_host_payload_only: bool = False) -> list[str]:
    tree = ast.parse(source)
    queue_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "queue" or parts[-1] == "queue":
                    queue_names.add(alias.asname or parts[-1])
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            from_queue_mod = mod == "queue" or mod.endswith(".queue")
            for alias in node.names:
                if alias.name == "queue" or from_queue_mod:
                    queue_names.add(alias.asname or alias.name)
    violations = []
    allowed_attrs = {"queue_payload"} if federation_host_payload_only else set()
    if not federation_host_payload_only:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    parts = alias.name.split(".")
                    if parts[0] == "queue" or parts[-1] == "queue":
                        violations.append(f"import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                from_queue_mod = mod == "queue" or mod.endswith(".queue")
                for alias in node.names:
                    if alias.name == "queue" or from_queue_mod:
                        violations.append(f"from-import {mod}:{alias.name}")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in queue_names:
            violations.append(f"call through queue alias {func.id!r}")
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if func.value.id in queue_names:
                if federation_host_payload_only:
                    if func.attr not in allowed_attrs:
                        violations.append(f"queue attribute {func.attr!r}")
                else:
                    violations.append(f"call queue.{func.attr}")
    return violations


def test_add_stores_0600_secrets_and_hash_only_record(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    r = _invoke(cfg, monkeypatch, "add", "box", "--url", "http://127.0.0.1:8792")
    assert r.exit_code == 0
    assert r.output.endswith("\n") and r.output.count("\n") == 1
    blob = json.loads(r.output.strip())
    assert set(blob) == {"hub_name", "host_name", "token", "secret"}
    assert blob["host_name"] == "box"
    assert blob["hub_name"] == socket.gethostname()
    tok_p = Path(cfg["config_dir"]) / "secrets/hosts/box.token"
    key_p = Path(cfg["config_dir"]) / "secrets/hosts/box.key"
    assert stat.S_IMODE(tok_p.stat().st_mode) == 0o600
    assert stat.S_IMODE(key_p.stat().st_mode) == 0o600
    assert stat.S_IMODE(tok_p.parent.stat().st_mode) == 0o700
    rec = H.load(cfg)[0]
    assert set(rec) == {"name", "url", "token_sha256", "secret_ref", "enabled", "status_cache"}
    assert rec["secret_ref"] == "secrets/hosts/box.key"
    assert rec["token_sha256"] == PJ.token_hash(blob["token"])
    assert tok_p.read_text() == blob["token"] + "\n"
    assert key_p.read_text() == blob["secret"] + "\n"
    r0 = _invoke(cfg, monkeypatch, "add", "pzero", "--url", "http://127.0.0.1:0")
    assert r0.exit_code == 0
    assert H.find(H.load(cfg), "pzero")["url"] == "http://127.0.0.1:0"
    for name, url in (("v6loop", "http://[::1]:8792"), ("v6tail", "http://[fd7a:115c:a1e0::1]:8792")):
        r6 = _invoke(cfg, monkeypatch, "add", name, "--url", url)
        assert r6.exit_code == 0, r6.output
        rec6 = H.find(H.load(cfg), name)
        assert rec6["url"] == url
        F.HubClient(rec6, cfg)
    assert H.find(H.load(cfg), "box")["url"] == "http://127.0.0.1:8792"


def test_write_host_secrets_rolls_back_token_on_key_failure(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    real = F._write_private_fsync
    calls = {"n": 0}

    def flaky(path, text):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("no space")
        return real(path, text)

    monkeypatch.setattr(F, "_write_private_fsync", flaky)
    with pytest.raises(OSError):
        F.write_host_secrets(cfg, "box", "tok", "sec")
    assert not F.host_token_path(cfg, "box").exists()
    assert not F.host_secret_path(cfg, "box").exists()


def test_add_rejects_duplicate_bad_name_and_bad_url(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    assert _invoke(cfg, monkeypatch, "add", "box", "--url", "http://127.0.0.1:8792").exit_code == 0
    snap_hosts = H.path(cfg).read_bytes()
    snap_secrets = {p: p.read_bytes() for p in Path(cfg["config_dir"]).glob("secrets/hosts/*")}
    assert _invoke(cfg, monkeypatch, "add", "box", "--url", "http://127.0.0.1:8792").exit_code != 0
    assert H.path(cfg).read_bytes() == snap_hosts
    assert {p: p.read_bytes() for p in Path(cfg["config_dir"]).glob("secrets/hosts/*")} == snap_secrets
    assert _invoke(cfg, monkeypatch, "add", "BAD", "--url", "http://x").exit_code != 0
    assert H.path(cfg).read_bytes() == snap_hosts
    assert {p: p.read_bytes() for p in Path(cfg["config_dir"]).glob("secrets/hosts/*")} == snap_secrets
    before = list(Path(cfg["config_dir"]).glob("secrets/hosts/*"))
    assert _invoke(cfg, monkeypatch, "add", "ok", "--url", "ftp://x").exit_code != 0
    assert list(Path(cfg["config_dir"]).glob("secrets/hosts/*")) == before
    for bad in ("http://", "http://:8080", "https://", "http://u:p@h:1", "http://h:1/?q=1",
                "http://h:1/#f", "http://h:1/a b"):
        assert _invoke(cfg, monkeypatch, "add", "ok2", "--url", bad).exit_code != 0, bad
    assert H.path(cfg).read_bytes() == snap_hosts
    assert {p: p.read_bytes() for p in Path(cfg["config_dir"]).glob("secrets/hosts/*")} == snap_secrets


def _refused_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_list_hides_secrets_and_missing_file_is_empty(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    assert _invoke(cfg, monkeypatch, "list").exit_code == 0
    assert _invoke(cfg, monkeypatch, "list").output == ""
    assert not H.path(cfg).exists()
    _invoke(cfg, monkeypatch, "add", "box", "--url", "http://127.0.0.1:1")
    out = _invoke(cfg, monkeypatch, "list").output.strip()
    assert out == "box\thttp://127.0.0.1:1\ton"
    assert "token" not in out


def test_load_empty_or_corrupt_hosts_json_is_empty(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    p = H.path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    for blob in ("", "[]", '{"schema":"other"}', "not json"):
        p.write_text(blob)
        assert H.load(cfg) == []
        assert _invoke(cfg, monkeypatch, "list").exit_code == 0
        assert _invoke(cfg, monkeypatch, "list").output == ""


def test_registry_fsync_failure_keeps_previous_bytes(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    H.update(cfg, lambda rs: rs.append({"name": "a", "url": "http://127.0.0.1:1",
                                          "token_sha256": "a" * 64,
                                          "secret_ref": "secrets/hosts/a.key",
                                          "enabled": True, "status_cache": None}))
    prev = H.path(cfg).read_bytes()
    raised = False

    def bad(fd):
        nonlocal raised
        raised = True
        raise OSError("nope")

    monkeypatch.setattr(os, "fsync", bad)
    with pytest.raises(OSError):
        H.update(cfg, lambda rs: rs.append({"name": "b", "url": "http://127.0.0.1:2",
                                            "token_sha256": "b" * 64,
                                            "secret_ref": "secrets/hosts/b.key",
                                            "enabled": True, "status_cache": None}))
    assert raised and H.path(cfg).read_bytes() == prev
    monkeypatch.undo()
    H.update(cfg, lambda rs: rs.append({"name": "b", "url": "http://127.0.0.1:2",
                                        "token_sha256": "b" * 64,
                                        "secret_ref": "secrets/hosts/b.key",
                                        "enabled": True, "status_cache": None}))
    assert len(H.load(cfg)) == 2


def test_registry_concurrent_updates_both_persist(tmp_path):
    cfg = _cfg(tmp_path)
    names = [f"h{i:02d}" for i in range(8)]

    def add_one(n):
        H.update(cfg, lambda rs, name=n: rs.append({
            "name": name, "url": f"http://127.0.0.1:{1000 + int(name[1:])}",
            "token_sha256": "a" * 64, "secret_ref": f"secrets/hosts/{name}.key",
            "enabled": True, "status_cache": None,
        }))

    threads = [threading.Thread(target=add_one, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(r["name"] for r in H.load(cfg)) == sorted(names)


def test_rm_deletes_secrets_prints_unenrol_and_makes_no_http(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _invoke(cfg, monkeypatch, "add", "box", "--url", "http://127.0.0.1:9")
    _invoke(cfg, monkeypatch, "add", "keep", "--url", "http://127.0.0.1:9")
    keep_before = {p.name: p.read_bytes() for p in Path(cfg["config_dir"]).glob("secrets/hosts/keep.*")}
    assert len(keep_before) == 2
    keep_rec = H.find(H.load(cfg), "keep")

    def no_dial(*a, **k):
        raise AssertionError("rm must not open a connection")

    monkeypatch.setattr(socket.socket, "connect", no_dial)
    monkeypatch.setattr(socket, "create_connection", no_dial)
    monkeypatch.setattr(http.client.HTTPConnection, "connect", no_dial)
    order = []
    real_fsync, real_unlink = os.fsync, Path.unlink
    monkeypatch.setattr(os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(Path, "unlink",
                        lambda self, *a, **k: (order.append(f"unlink:{self.name}"),
                                               real_unlink(self, *a, **k))[1])
    r = _invoke(cfg, monkeypatch, "rm", "box")
    assert r.exit_code == 0
    assert "run nw hosts unenrol on the host" in r.output
    assert order.index("fsync") < order.index("unlink:box.token")
    assert order.index("fsync") < order.index("unlink:box.key")
    assert not F.host_token_path(cfg, "box").exists()
    assert not F.host_secret_path(cfg, "box").exists()
    assert [x["name"] for x in H.load(cfg)] == ["keep"]
    assert H.find(H.load(cfg), "keep") == keep_rec
    assert {p.name: p.read_bytes() for p in Path(cfg["config_dir"]).glob("secrets/hosts/keep.*")} == keep_before
    r_unknown = _invoke(cfg, monkeypatch, "rm", "nobody")
    assert r_unknown.exit_code != 0
    assert "run nw hosts unenrol" not in r_unknown.output
    assert [x["name"] for x in H.load(cfg)] == ["keep"]


def test_on_off_sets_enabled(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _invoke(cfg, monkeypatch, "add", "box", "--url", "http://127.0.0.1:1")
    snap = H.path(cfg).read_bytes()
    assert _invoke(cfg, monkeypatch, "off", "box").exit_code == 0
    assert _invoke(cfg, monkeypatch, "off", "box").output == ""
    assert H.load(cfg)[0]["enabled"] is False
    assert _invoke(cfg, monkeypatch, "list").output.strip() == "box\thttp://127.0.0.1:1\toff"
    assert _invoke(cfg, monkeypatch, "on", "box").exit_code == 0
    assert _invoke(cfg, monkeypatch, "on", "box").output == ""
    assert H.load(cfg)[0]["enabled"] is True
    assert _invoke(cfg, monkeypatch, "off", "ghost").exit_code != 0
    assert H.path(cfg).read_bytes() == snap
    assert _invoke(cfg, monkeypatch, "on", "ghost").exit_code != 0
    assert H.path(cfg).read_bytes() == snap


def test_enrol_stdin_only_0600(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    blob = {"hub_name": "h", "host_name": "box", "token": "t", "secret": "s"}
    env = {**os.environ, "NW_DATA_DIR": cfg["data_dir"], "NW_CONFIG_DIR": cfg["config_dir"]}
    r = CliRunner().invoke(cli, ["hosts", "enrol"], input=json.dumps(blob) + "\n", env=env)
    assert r.exit_code == 0 and r.output == ""
    p = F.federation_path(cfg)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert json.loads(p.read_text()) == blob
    fail = [
        (["hosts", "enrol", json.dumps({**blob, "token": "pos"})], None),
        (["hosts", "enrol", json.dumps({**blob, "token": "pos"})], json.dumps(blob) + "\n"),
        (["hosts", "enrol", "--blob", json.dumps({**blob, "token": "pos"})], None),
        (["hosts", "enrol"], "\n"),
        (["hosts", "enrol"], "[]\n"),
        (["hosts", "enrol"], json.dumps({"hub_name": "h"}) + "\n"),
        (["hosts", "enrol"], json.dumps({**blob, "extra": "x"}) + "\n"),
        (["hosts", "enrol"], json.dumps({**blob, "token": ""}) + "\n"),
    ]
    for args, inp in fail:
        before = p.read_bytes()
        rr = CliRunner().invoke(cli, list(args), input=inp, env=env)
        assert rr.exit_code != 0
        assert p.read_bytes() == before
    blob2 = {**blob, "token": "t2"}
    assert CliRunner().invoke(cli, ["hosts", "enrol"], input=json.dumps(blob2) + "\n", env=env).exit_code == 0
    assert json.loads(p.read_text()) == blob2
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert CliRunner().invoke(cli, ["hosts", "unenrol"], env=env).exit_code == 0
    assert not p.exists()
    assert CliRunner().invoke(cli, ["hosts", "unenrol"], env=env).exit_code == 0
    assert set(cli_hosts.hosts_group.commands) == {
        "add", "list", "rm", "on", "off", "check", "enrol", "unenrol",
    }
    help_out = CliRunner().invoke(cli, ["hosts", "--help"]).output
    assert "revoke" not in help_out


def test_serve_federate_refuses_without_federation_json(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    binds = []
    orig_bind = socket.socket.bind

    def track_bind(self, addr):
        binds.append(addr)
        return orig_bind(self, addr)

    monkeypatch.setattr(socket.socket, "bind", track_bind)

    def refuse():
        r = CliRunner().invoke(cli, ["serve", "--federate"])
        assert r.exit_code != 0
        assert str(F.federation_path(cfg)) in r.output
        assert not binds

    refuse()
    binds.clear()
    F.federation_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    F.federation_path(cfg).write_text("not json")
    refuse()
    binds.clear()
    F.federation_path(cfg).write_text("[]")
    refuse()
    binds.clear()
    F.federation_path(cfg).write_text(json.dumps({"hub_name": "", "host_name": "x",
                                                  "token": "t", "secret": "s"}))
    refuse()


def test_unenrol_running_server_returns_unsigned_401(tmp_path, monkeypatch):
    hub = _cfg(tmp_path, "hub")
    host = _cfg(tmp_path, "host")
    blob = _add_and_enrol(host, hub, monkeypatch)
    assert F.read_federation(host)
    srv, url = _serve(host)
    _point_hub(hub, "box", url)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    F.federation_path(host).unlink()
    st, hdrs, _ = _raw("GET", base, "/v1/status", blob["token"], blob["secret"], headers={})
    assert st == 401
    lowers = {k.lower() for k in hdrs}
    assert "x-nw-sig" not in lowers and "x-nw-sig-ts" not in lowers
    st2, hdrs2, _ = _raw("GET", base, "/v1/status", blob["token"], blob["secret"])
    assert st2 == 401
    lowers2 = {k.lower() for k in hdrs2}
    assert "x-nw-sig" not in lowers2 and "x-nw-sig-ts" not in lowers2
    st3, hdrs3, _ = _raw("HEAD", base, "/v1/status", blob["token"], blob["secret"], headers={})
    assert st3 == 401 and "x-nw-sig" not in {k.lower() for k in hdrs3}
    srv.shutdown()


def test_wire_mac_is_the_documented_tagged_formula(tmp_path, monkeypatch):
    """A client hand-built from the documented formulas alone (no `federation` helpers) gets 200,
    and the response verifies by the documented response formula."""
    import hashlib
    import hmac
    import time

    monkeypatch.setattr(time, "time", lambda: NOW)
    host = _cfg(tmp_path, "host")
    secret, path, ts = "s3cret", "/v1/status", int(NOW)
    F.write_federation(host, {"hub_name": "h", "host_name": "box", "token": "tok", "secret": secret})
    srv, base = _serve(host)

    def mac(line):
        return hmac.new(secret.encode(), line.encode(), hashlib.sha256).hexdigest()

    def sha(b):
        return hashlib.sha256(b).hexdigest()

    req_line = f"nw-req|GET|{path}|{sha(b'')}|{ts}"
    assert F.sign(secret, "GET", path, b"", ts, direction="req") == mac(req_line)
    by_hand = {"Authorization": "Bearer tok", "X-NW-Req-Ts": str(ts),
               "X-NW-Req-Sig": mac(req_line), "X-NW-Sig-V": "2"}
    st, h, raw = _raw("GET", base, path, "tok", secret, headers=by_hand)
    assert st == 200 and h["X-NW-Sig-V"] == "2"
    resp_line = f"nw-resp|200|GET|{path}|{sha(raw)}|{h['X-NW-Sig-Ts']}"
    assert h["X-NW-Sig"] == mac(resp_line)
    # the old four-field construction is no longer a request MAC
    old = mac(f"GET|{path}|{sha(b'')}|{ts}")
    st, h, raw = _raw("GET", base, path, "tok", secret, headers={**by_hand, "X-NW-Req-Sig": old})
    assert st == 401 and json.loads(raw) == {"error": "unauthorized"}
    _resp_sig(secret, "GET", path, h, raw, 401)
    srv.shutdown()


def test_sign_and_verify_demand_a_direction_and_valid_status():
    args = ("abc", "GET", "/v1/status", b"", 1700000000)
    with pytest.raises(TypeError):
        F.sign(*args)
    with pytest.raises(TypeError):
        F.verify(*args[:4], "00", args[4], now=NOW)
    for bad in ("request", "", None, "REQ"):
        with pytest.raises(ValueError):
            F.sign(*args, direction=bad)
    with pytest.raises(ValueError):
        F.sign(*args, direction="resp")
    for bad in (99, 600, "200", 200.0, True, None):
        with pytest.raises(ValueError):
            F.sign(*args, direction="resp", status=bad)
    with pytest.raises(ValueError):
        F.sign(*args, direction="req", status=200)
    with pytest.raises(ValueError):
        F.verify(*args[:4], "00", args[4], direction="req", status=200, now=NOW)
    for st in (100, 200, 401, 599):
        assert F.sign(*args, direction="resp", status=st) != F.sign(*args, direction="req")


def _federation_sign_verify_violations(source: str, *, in_federation: bool) -> list[str]:
    """Calls to federation's `sign`/`verify` in `source` that lack a `direction=` keyword."""
    tree = ast.parse(source)
    mods, names = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "nightwright.agentic" or (node.level and node.module is None):
                mods |= {a.asname or a.name for a in node.names if a.name == "federation"}
            if node.module and node.module.split(".")[-1] == "federation":
                names |= {a.asname or a.name for a in node.names if a.name in ("sign", "verify")}
        elif isinstance(node, ast.Import):
            mods |= {a.asname for a in node.names
                     if a.name == "nightwright.agentic.federation" and a.asname}
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        hit = ((isinstance(f, ast.Name) and f.id in ({"sign", "verify"} if in_federation else set())
                | names)
               or (isinstance(f, ast.Attribute) and f.attr in ("sign", "verify")
                   and isinstance(f.value, ast.Name) and f.value.id in mods | {"federation"}))
        if hit and not any(k.arg == "direction" for k in node.keywords):
            bad.append(f"line {node.lineno}")
    return bad


def test_every_federation_sign_verify_call_passes_direction():
    root = Path(__file__).resolve().parent.parent / "src" / "nightwright"
    seen_fed = False
    for py in sorted(root.rglob("*.py")):
        in_fed = py.name == "federation.py" and py.parent.name == "agentic"
        seen_fed = seen_fed or in_fed
        assert _federation_sign_verify_violations(py.read_text(encoding="utf-8"),
                                                  in_federation=in_fed) == [], py
    assert seen_fed
    # the checker itself: it flags a missing keyword and ignores look-alikes
    assert _federation_sign_verify_violations(
        "from nightwright.agentic import federation as F\nF.sign(1)\nF.verify(1, direction='req')\n"
        "import hmac\nhmac.verify(1)\n", in_federation=False) == ["line 2"]
    assert _federation_sign_verify_violations("sign(1)\nverify(2)\n", in_federation=True) == [
        "line 1", "line 2"]
    assert _federation_sign_verify_violations("sign(1)\n", in_federation=False) == []


def test_hmac_test_vector(tmp_path):
    assert F.sign("abc", "GET", "/v1/status", b"", 1700000000, direction="req") == (
        "a4879c456e4e8357b2b486928f5d4ed18b6a92754b581fc173ba3e787f56f61b")
    assert F.sign("abc", "GET", "/v1/status", b"", 1700000000, direction="resp", status=200) == (
        "9a6a671ded7c7fdcd11b3b6fd50c812a6f0a8b73724caa3ae8b8106ee9e8b8dd")
    assert F.verify("abc", "GET", "/v1/status", b"", None, 1700000000, direction="req",
                    now=NOW) == "bad signature"
    cfg = _cfg(tmp_path)
    sec_p = F.host_secret_path(cfg, "_nosuch")
    sec_p.parent.mkdir(parents=True, exist_ok=True)
    sec_p.write_text("abc\n")
    assert F.read_host_secret(cfg, "_nosuch") == "abc"


def test_enrolled_request_auth(tmp_path, monkeypatch):
    import time
    monkeypatch.setattr(time, "time", lambda: NOW)
    host = _cfg(tmp_path, "host")
    F.write_federation(host, {"hub_name": "h", "host_name": "box", "token": "tok", "secret": "abc"})
    srv, base = _serve(host)
    port = srv.server_address[1]

    def req(method, path, body=b"", **kw):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(method, path, body=body, headers=kw.get("headers", {}))
        r = conn.getresponse()
        raw = r.read()
        h = dict(r.getheaders())
        conn.close()
        return r.status, h, raw

    secret = "abc"
    st, h, raw = req("GET", "/v1/status")
    assert st == 401
    _resp_sig(secret, "GET", "/v1/status", h, raw, st)
    no_sig = {"Authorization": "Bearer tok", "X-NW-Req-Ts": str(int(NOW))}
    st, h, raw = req("GET", "/v1/status", headers=no_sig)
    assert st == 401
    _resp_sig(secret, "GET", "/v1/status", h, raw, st)
    no_ts = {**F.request_headers(secret, "GET", "/v1/status", b"", now=NOW),
             "Authorization": "Bearer tok"}
    del no_ts["X-NW-Req-Ts"]
    st, h, raw = req("GET", "/v1/status", headers=no_ts)
    assert st == 401
    _resp_sig(secret, "GET", "/v1/status", h, raw, st)
    hdrs = {**F.request_headers(secret, "GET", "/v1/status", b"", now=NOW), "Authorization": "Bearer tok"}
    st, h, raw = req("GET", "/v1/status", headers=hdrs)
    assert st == 200
    hdrs_bad = {**F.request_headers(secret, "GET", "/v1/status", b"", now=NOW), "Authorization": "Bearer wrong"}
    st, h, raw = req("GET", "/v1/status", headers=hdrs_bad)
    assert st == 401
    _resp_sig(secret, "GET", "/v1/status", h, raw, st)
    hdrs = {**F.request_headers(secret, "GET", "/v1/status", b"", now=NOW + 121), "Authorization": "Bearer tok"}
    st, h, raw = req("GET", "/v1/status", headers=hdrs)
    assert st == 401
    _resp_sig(secret, "GET", "/v1/status", h, raw, st)
    hdrs_past = {**F.request_headers(secret, "GET", "/v1/status", b"", now=NOW - 121),
                 "Authorization": "Bearer tok"}
    st, h, raw = req("GET", "/v1/status", headers=hdrs_past)
    assert st == 401
    _resp_sig(secret, "GET", "/v1/status", h, raw, st)
    hdrs = {**F.request_headers(secret, "GET", "/v1/status", b"", now=NOW + 120), "Authorization": "Bearer tok"}
    assert req("GET", "/v1/status", headers=hdrs)[0] == 200
    hdrs_p120 = {**F.request_headers(secret, "GET", "/v1/status", b"", now=NOW - 120),
                 "Authorization": "Bearer tok"}
    assert req("GET", "/v1/status", headers=hdrs_p120)[0] == 200
    body = b"x"
    hdrs = {**F.request_headers(secret, "GET", "/v1/status", body, now=NOW), "Authorization": "Bearer tok"}
    # A GET carrying a body is refused even with a matching MAC (signed 401): a signed response
    # must never be replayable as a request.
    st, h, raw = req("GET", "/v1/status", body=body, headers={**hdrs, "Content-Length": "1"})
    assert st == 401
    _resp_sig(secret, "GET", "/v1/status", h, raw, st)
    # Reflection: lift a signed 401's ts/sig and replay them as the request MAC over its body.
    st, h, raw401 = req("GET", "/v1/status", headers={"Authorization": "Bearer wrong"})
    assert st == 401
    replay = {"Authorization": "Bearer tok", "X-NW-Req-Ts": h["X-NW-Sig-Ts"],
              "X-NW-Req-Sig": h["X-NW-Sig"], "Content-Length": str(len(raw401))}
    st, h2, raw2 = req("GET", "/v1/status", body=raw401, headers=replay)
    assert st == 401
    assert b"schema" not in raw2
    _resp_sig(secret, "GET", "/v1/status", h2, raw2, st)
    hdrs_empty = {**F.request_headers(secret, "GET", "/v1/status", b"", now=NOW), "Authorization": "Bearer tok"}
    st, h, raw = req("GET", "/v1/status", body=body, headers={**hdrs_empty, "Content-Length": "1"})
    assert st == 401
    _resp_sig(secret, "GET", "/v1/status", h, raw, st)
    tampered = {**hdrs, "X-NW-Req-Sig": hdrs["X-NW-Req-Sig"][:-1] + ("0" if hdrs["X-NW-Req-Sig"][-1] != "0" else "1")}
    st, h, raw = req("GET", "/v1/status", headers=tampered)
    assert st == 401
    _resp_sig(secret, "GET", "/v1/status", h, raw, st)
    post_body = b"!"
    post_hdrs = {**F.request_headers(secret, "POST", "/v1/status", post_body, now=NOW), "Authorization": "Bearer tok",
                 "Content-Length": "1"}
    st, h, raw = req("POST", "/v1/status", body=b"?", headers=post_hdrs)
    assert st == 401
    _resp_sig(secret, "POST", "/v1/status", h, raw, st)
    # The MAC binds the body on a route that reads one: a POST signed over its own body passes the
    # MAC (then 405, the method is not allowed); signed over b"" but sent with a body it is a 401.
    for sign_over, want in ((b"x", 405), (b"", 401), (b"y", 401)):
        bh = {**F.request_headers(secret, "POST", "/v1/status", sign_over, now=NOW),
              "Authorization": "Bearer tok", "Content-Length": "1"}
        st, h, raw = req("POST", "/v1/status", body=b"x", headers=bh)
        assert st == want, sign_over
        _resp_sig(secret, "POST", "/v1/status", h, raw, st)
    # A GET/HEAD with a body or a Transfer-Encoding, and a body over the cap, are signed 401s that
    # never reach the status document.
    big = str(FH.MAX_BODY_BYTES + 1)
    refused = [
        ("GET", {"Content-Length": "1"}, b"x"),
        ("HEAD", {"Content-Length": "1"}, b"x"),
        ("GET", {"Transfer-Encoding": "chunked"}, b""),
        ("HEAD", {"Transfer-Encoding": "chunked"}, b""),
        ("POST", {"Content-Length": big}, b""),
    ]
    for method, extra, rbody in refused:
        rh = {**F.request_headers(secret, method, "/v1/status", rbody, now=NOW),
              "Authorization": "Bearer tok", **extra}
        st, h, raw = req(method, "/v1/status", body=rbody, headers=rh)
        assert st == 401, (method, extra)
        assert b"schema" not in raw
        _resp_sig(secret, method, "/v1/status", h, b"" if method == "HEAD" else raw, st)
    post_hdrs = {**F.request_headers(secret, "POST", "/v1/status", b"", now=NOW), "Authorization": "Bearer tok"}
    st, h, raw = req("POST", "/v1/status", headers=post_hdrs)
    assert st == 405
    _resp_sig(secret, "POST", "/v1/status", h, raw, st)
    err405 = json.dumps({"error": "method_not_allowed"}, separators=(",", ":")).encode()
    for method in ("HEAD", "PATCH"):
        m_hdrs = {**F.request_headers(secret, method, "/v1/status", b"", now=NOW), "Authorization": "Bearer tok"}
        st, h, raw = req(method, "/v1/status", headers=m_hdrs)
        assert st == 405
        sig_body = b"" if method == "HEAD" else raw
        _resp_sig(secret, method, "/v1/status", h, sig_body, st)
    get_hdrs = {**F.request_headers(secret, "GET", "/nope", b"", now=NOW), "Authorization": "Bearer tok"}
    st, h, raw = req("GET", "/nope", headers=get_hdrs)
    assert st == 404
    _resp_sig(secret, "GET", "/nope", h, raw, st)
    patch_hdrs = {**F.request_headers(secret, "PATCH", "/nope", b"", now=NOW), "Authorization": "Bearer tok"}
    st, h, raw = req("PATCH", "/nope", headers=patch_hdrs)
    assert st == 404
    _resp_sig(secret, "PATCH", "/nope", h, raw, st)
    rev_hdrs = {**F.request_headers(secret, "POST", "/v1/revoke", b"", now=NOW), "Authorization": "Bearer tok"}
    st, h, raw = req("POST", "/v1/revoke", headers=rev_hdrs)
    assert st == 404
    _resp_sig(secret, "POST", "/v1/revoke", h, raw, st)
    qpath = "/v1/status?z=1&a=2"
    q_hdrs = {**F.request_headers(secret, "GET", qpath, b"", now=NOW), "Authorization": "Bearer tok"}
    st, h, raw = req("GET", qpath, headers=q_hdrs)
    assert st == 200
    _resp_sig(secret, "GET", qpath, h, raw, st)
    bad_q = {**F.request_headers(secret, "GET", "/v1/status?a=2&z=1", b"", now=NOW),
             "Authorization": "Bearer tok"}
    st, h, raw = req("GET", qpath, headers=bad_q)
    assert st == 401
    _resp_sig(secret, "GET", qpath, h, raw, st)
    unicode_ts = {**F.request_headers(secret, "GET", "/v1/status", b"", now=NOW),
                "Authorization": "Bearer tok", "X-NW-Req-Ts": "\u00b2"}
    st, h, raw = req("GET", "/v1/status", headers=unicode_ts)
    assert st == 401
    _resp_sig(secret, "GET", "/v1/status", h, raw, st)
    lz = {**F.request_headers(secret, "GET", "/v1/status", b"", now=NOW),
          "Authorization": "Bearer tok", "X-NW-Req-Ts": "01700000000"}
    st, h, raw = req("GET", "/v1/status", headers=lz)
    assert st == 401
    _resp_sig(secret, "GET", "/v1/status", h, raw, st)
    lo = {**F.request_headers(secret, "GET", "/v1/status", b"", now=NOW), "Authorization": "Bearer tok"}
    lo["x-nw-req-sig"] = lo.pop("X-NW-Req-Sig")
    lo["x-nw-req-ts"] = lo.pop("X-NW-Req-Ts")
    assert req("GET", "/v1/status", headers=lo)[0] == 200
    cl = {**F.request_headers(secret, "GET", "/v1/status", b"", now=NOW), "Authorization": "Bearer tok",
          "Content-Length": "\u00b2"}
    st, h, raw = req("GET", "/v1/status", headers=cl)
    assert st == 200
    _resp_sig(secret, "GET", "/v1/status", h, raw, st)
    ok = {**F.request_headers(secret, "GET", "/v1/status", b"", now=NOW), "Authorization": "Bearer tok"}
    assert req("GET", "/v1/status", headers=ok)[0] == 200
    srv.shutdown()


def test_request_mac_is_enforced(tmp_path, monkeypatch):
    """Security: the request MAC alone decides entry once the bearer token is right."""
    import time

    monkeypatch.setattr(time, "time", lambda: NOW)
    host = _cfg(tmp_path, "host")
    F.write_federation(host, {"hub_name": "h", "host_name": "box", "token": "tok", "secret": "abc"})
    srv, base = _serve(host)
    secret, path = "abc", "/v1/status"

    def go(method="GET", target=path, body=b"", drop=(), mutate=None, sign_for=None):
        sign_method, sign_target, sign_body = sign_for or (method, target, body)
        hdrs = {"Authorization": "Bearer tok",
                **F.request_headers(secret, sign_method, sign_target, sign_body, now=NOW)}
        if mutate:
            hdrs = mutate(hdrs)
        for k in drop:
            hdrs.pop(k)
        if body:
            hdrs["Content-Length"] = str(len(body))
        return _raw(method, base, target, "tok", secret, body, headers=hdrs)

    # correct MAC: accepted, and the 200 is itself signed
    st, h, raw = go()
    assert st == 200
    _resp_sig(secret, "GET", path, h, raw, st)

    def flip(hdrs):
        sig = hdrs["X-NW-Req-Sig"]
        return {**hdrs, "X-NW-Req-Sig": ("0" if sig[0] != "0" else "1") + sig[1:]}

    def wrong_secret(hdrs):
        return {**hdrs, "X-NW-Req-Sig": F.sign("not-abc", "GET", path, b"", int(NOW),
                                                direction="req")}

    rejected = {
        "wrong MAC (flipped)": go(mutate=flip),
        "wrong MAC (other secret)": go(mutate=wrong_secret),
        "missing signature header": go(drop=("X-NW-Req-Sig",)),
        "empty signature header": go(mutate=lambda x: {**x, "X-NW-Req-Sig": ""}),
        "valid header, tampered body": go(body=b"tampered", sign_for=("GET", path, b"original")),
        "valid header, other path": go(sign_for=("GET", "/v1/other", b"")),
        "valid header, other method": go(sign_for=("POST", path, b"")),
    }
    for label, (st, h, raw) in rejected.items():
        assert st == 401, label
        assert json.loads(raw) == {"error": "unauthorized"}, label
        _resp_sig(secret, "GET", path, h, raw, st)  # enrolled: the 401 is signed

    # not enrolled any more: the 401 carries no signature headers at all
    F.federation_path(host).unlink()
    for st, h, _ in (go(), go(drop=("X-NW-Req-Sig",)), go(mutate=flip)):
        assert st == 401
        assert not {k.lower() for k in h} & {"x-nw-sig", "x-nw-sig-ts"}
    srv.shutdown()


def test_handler_bad_request_target_no_traceback(tmp_path, monkeypatch):
    import time

    monkeypatch.setattr(time, "time", lambda: NOW)
    host = _cfg(tmp_path, "host")
    F.write_federation(host, {"hub_name": "h", "host_name": "box", "token": "tok", "secret": "abc"})
    srv, _ = _serve(host)
    port = srv.server_address[1]
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.sendall(b"GET http://[::1/v1/status HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    buf = sock.recv(4096)
    sock.close()
    assert buf.startswith(b"HTTP/1.1 401")
    srv.shutdown()


def test_hubclient_rejects_bad_responses(tmp_path, monkeypatch):
    import http.server
    import time

    monkeypatch.setattr(time, "time", lambda: NOW)
    hub = _cfg(tmp_path, "hub")
    host = _cfg(tmp_path, "host")
    blob = _add_and_enrol(host, hub, monkeypatch)
    srv, url = _serve(host)
    _point_hub(hub, "box", url)
    rec = H.load(hub)[0]
    secret = blob["secret"]
    client = F.HubClient(rec, hub, now=NOW)
    assert client.CONNECT_TIMEOUT == 2 and client.READ_TIMEOUT == 2
    connect_timeouts = []
    read_timeouts = []
    client_socks: set[int] = set()
    real_connect = socket.socket.connect

    def track_connect(self, addr):
        connect_timeouts.append(self.gettimeout())
        client_socks.add(id(self))
        return real_connect(self, addr)

    monkeypatch.setattr(socket.socket, "connect", track_connect)
    real_settimeout = socket.socket.settimeout

    def track_st(self, timeout):
        if id(self) in client_socks:
            read_timeouts.append(timeout)
        return real_settimeout(self, timeout)

    monkeypatch.setattr(socket.socket, "settimeout", track_st)
    for _ in range(3):
        assert client.get_json("/v1/status")["schema"] == "nw.federation.status.v1"
    assert len(connect_timeouts) == 3
    assert all(1.0 < t <= 2 for t in connect_timeouts)
    read_phase = [t for t in read_timeouts if t is not None and 0 < t <= 2]
    assert len(read_phase) >= 3
    assert any(1.0 < t <= 2 for t in read_phase)
    rec2 = {**rec, "url": rec["url"] + "/"}
    F.HubClient(rec2, hub, now=NOW).get_json("/v1/status")
    with pytest.raises(ConnectionError):
        F.HubClient(rec, hub, now=NOW).request("GET", "/v1/status-not-really")

    bad_mode = ["unsigned"]

    class _Bad(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = b'{"schema":"nw.federation.status.v1","projects":[],"campaigns":[]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            if bad_mode[0] == "tampered":
                hdrs = F.response_headers(secret, "GET", "/v1/status", body, status=200, now=NOW)
                for k, v in hdrs.items():
                    self.send_header(k, v)
                body = body[:20] + bytes([body[20] ^ 0x01]) + body[21:]
            elif bad_mode[0] == "skew121":
                hdrs = F.response_headers(secret, "GET", "/v1/status", body, status=200, now=NOW)
                for k, v in hdrs.items():
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    bad_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Bad)
    th = threading.Thread(target=bad_srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    th.start()
    bad_url = f"http://127.0.0.1:{bad_srv.server_address[1]}"
    bad_rec = {**rec, "url": bad_url}
    with pytest.raises(ConnectionError):
        F.HubClient(bad_rec, hub, now=NOW).get_json("/v1/status")
    bad_mode[0] = "tampered"
    with pytest.raises(ConnectionError):
        F.HubClient(bad_rec, hub, now=NOW).get_json("/v1/status")
    bad_mode[0] = "skew121"
    with pytest.raises(ConnectionError):
        F.HubClient(bad_rec, hub, now=NOW + 121).get_json("/v1/status")
    with pytest.raises(ConnectionError):
        F.HubClient(bad_rec, hub, now=NOW - 121).get_json("/v1/status")
    F.HubClient(rec, hub, now=NOW + 120).get_json("/v1/status")
    F.HubClient(rec, hub, now=NOW - 120).get_json("/v1/status")
    bad_srv.shutdown()
    srv.shutdown()


def test_status_maps_queue_payload_items(tmp_path, monkeypatch):
    import time
    monkeypatch.setattr(time, "time", lambda: NOW)
    repo = make_repo(tmp_path)
    host = make_cfg(tmp_path, repo, script(tmp_path, []))
    _campaign(host, repo, "SPEC-1z", "QUEUED", project="Zeta")
    _campaign(host, repo, "SPEC-1", "QUEUED", project="P1")
    _campaign(host, repo, "SPEC-1b", "STARTED", project="P1")
    _campaign(host, repo, "SPEC-1c", "QUEUED", project="Alpha")
    _campaign(host, repo, "SPEC-1d", "QUEUED", project="Alpha")
    _campaign(host, repo, "SPEC-1e", "QUEUED", project="")
    _campaign(host, repo, "SPEC-1f", "QUEUED", project=None)
    _campaign(host, repo, "SPEC-2", "ABANDONED", project="P2")
    _campaign(host, repo, "SPEC-3", "MERGED", project="Pterm")
    F.write_federation(host, {"hub_name": "h", "host_name": "box", "token": "t", "secret": "abc"})
    srv, _ = _serve(host)
    payload = Q.queue_payload(host)
    doc = FH.status_document(host)
    assert doc["schema"] == "nw.federation.status.v1"
    want_items = [
        {k: item.get(k) for k in ("campaign", "project", "state", "spec_id", "kind")}
        for item in payload["items"]
    ]
    assert len(want_items) >= 4
    assert doc["campaigns"] == want_items
    assert [c["campaign"] for c in doc["campaigns"]] == [c["campaign"] for c in want_items]
    assert doc["projects"] == ["Alpha", "P1", "Zeta"]
    ids = [c["campaign"] for c in doc["campaigns"]]
    assert {"SPEC-1e", "SPEC-1f", "SPEC-1z"} <= set(ids)
    assert not {"SPEC-2", "SPEC-3"} & set(ids)
    port = srv.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port)
    st_hdrs = {**F.request_headers("abc", "GET", "/v1/status", b"", now=NOW), "Authorization": "Bearer t"}
    conn.request("GET", "/v1/status", headers=st_hdrs)
    resp = conn.getresponse()
    raw = resp.read()
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "application/json"
    assert json.loads(raw) == doc
    conn = http.client.HTTPConnection("127.0.0.1", port)
    hdrs = {**F.request_headers("abc", "GET", "/v1/lease", b"", now=NOW), "Authorization": "Bearer t"}
    conn.request("GET", "/v1/lease", headers=hdrs)
    resp = conn.getresponse()
    lease_raw = resp.read()
    lease_hdrs = dict(resp.getheaders())
    assert resp.status == 404
    _resp_sig("abc", "GET", "/v1/lease", lease_hdrs, lease_raw, resp.status)
    assert not isinstance(srv, PJ.PullServer)
    assert not list(_lease_checker_threads())
    srv.shutdown()
    srv.server_close()


def test_bind_and_serve_flags(tmp_path, monkeypatch):
    host = _cfg(tmp_path, "host")
    F.write_federation(host, {"hub_name": "h", "host_name": "b", "token": "t", "secret": "s"})
    for bind in ("0.0.0.0:8792", "[::]:8792", "8.8.8.8:8792"):
        with pytest.raises(ValueError):
            FH.make_federate_server(host, bind)
    srv = FH.make_federate_server(host, "127.0.0.1:0")
    srv.server_close()
    assert FH.DEFAULT_BIND == "127.0.0.1:8792"
    monkeypatch.setattr("nightwright.cli.load_config", lambda: host)

    def _no_server(*a, **kw):
        raise AssertionError("a server was constructed for an invalid mode combination")

    with monkeypatch.context() as m:
        m.setattr(FH, "make_federate_server", _no_server)
        m.setattr(PJ, "make_server", _no_server)
        # Exactly one of --jobs, --dashboard, --federate: none, and any two, are usage errors.
        for flags in ([], ["--jobs", "--dashboard"], ["--jobs", "--federate"],
                      ["--dashboard", "--federate"], ["--jobs", "--dashboard", "--federate"]):
            r_mode = CliRunner().invoke(cli, ["serve", *flags])
            assert r_mode.exit_code == 2, (flags, r_mode.output)
            assert "exactly one of" in r_mode.output, (flags, r_mode.output)
    cert, key = _mini_tls_pair(tmp_path)
    with pytest.raises(ValueError, match="tls-cert"):
        FH.make_federate_server(host, "127.0.0.1:0", tls_cert=cert, tls_key=None)
    with pytest.raises(ValueError, match="client-ca"):
        FH.make_federate_server(host, "127.0.0.1:0", client_ca=cert)
    FH.make_federate_server(host, "127.0.0.1:0", tls_cert=cert, tls_key=key).server_close()
    orig_bind = socket.socket.bind

    def lax_bind(self, addr):
        host = addr[0].strip("[]")
        if host in ("100.64.0.1", "8.8.8.8") or host.startswith("fd7a:"):
            return
        return orig_bind(self, addr)

    monkeypatch.setattr(socket.socket, "bind", lax_bind)
    FH.make_federate_server(host, "100.64.0.1:0").server_close()
    FH.make_federate_server(host, "[fd7a:115c:a1e0::1]:0").server_close()
    FH.make_federate_server(host, "8.8.8.8:8792", tls_cert=cert, tls_key=key).server_close()
    monkeypatch.setattr(socket.socket, "bind", orig_bind)
    ca_only = tmp_path / "ca.pem"
    ca_only.write_text("dummy\n")
    cli_binds: list = []
    real_make_cli = FH.make_federate_server

    def track_cli_bind(cfg, bind, **kw):
        cli_binds.append(bind)
        return real_make_cli(cfg, bind, **kw)

    monkeypatch.setattr(FH, "make_federate_server", track_cli_bind)
    r_ca = CliRunner().invoke(cli, ["serve", "--federate", "--client-ca", str(ca_only)])
    assert r_ca.exit_code == 2
    assert "--client-ca" in r_ca.output
    assert not cli_binds
    assert CliRunner().invoke(cli, ["serve", "--federate", "--tls-cert", cert]).exit_code != 0
    assert CliRunner().invoke(cli, ["serve", "--federate", "--tls-key", key]).exit_code != 0

    def puff(self, *a, **kw):
        raise KeyboardInterrupt()

    binds_used = []
    real_make2 = FH.make_federate_server

    def capture_bind(cfg, bind, **kw):
        binds_used.append(bind)
        # record the requested bind, but never take the real default port: a machine
        # that already runs a federate server on 8792 must not fail this test
        return real_make2(cfg, "127.0.0.1:0" if bind == FH.DEFAULT_BIND else bind, **kw)

    monkeypatch.setattr(FH, "make_federate_server", capture_bind)
    monkeypatch.setattr(FH.FederateServer, "serve_forever", puff)
    r = CliRunner().invoke(cli, ["serve", "--federate", "--bind", "127.0.0.1:0"])
    assert r.exit_code == 0
    assert "nw serve: federation on" in r.stderr
    binds_used.clear()
    r_def = CliRunner().invoke(cli, ["serve", "--federate"])
    assert r_def.exit_code == 0
    assert binds_used == [FH.DEFAULT_BIND]
    real_make = FH.make_federate_server

    def _v6_echo_server(cfg, bind, **kw):
        srv = real_make(cfg, "127.0.0.1:0", **kw)
        srv.server_address = ("::1", srv.server_address[1])
        return srv

    monkeypatch.setattr(FH, "make_federate_server", _v6_echo_server)
    r6 = CliRunner().invoke(cli, ["serve", "--federate", "--bind", "[::1]:0"])
    assert r6.exit_code == 0
    assert "nw serve: federation on http://[::1]:" in r6.stderr


def test_check_overlap_unreachable_and_disabled(tmp_path, monkeypatch):
    import time
    monkeypatch.setattr(time, "time", lambda: NOW)
    fetched_at = "2030-06-01T12:00:00+00:00"
    monkeypatch.setattr("nightwright.agentic.cli_hosts.now_iso", lambda: fetched_at)
    hub = _cfg(tmp_path, "hub")
    empty = _cfg(tmp_path, "empty")
    empty["config_dir"] = hub["config_dir"]
    empty["data_dir"] = hub["data_dir"]
    assert _invoke(empty, monkeypatch, "check").exit_code == 0
    assert not H.path(hub).exists()

    repo = make_repo(tmp_path)
    hub_full = make_cfg(tmp_path, repo, script(tmp_path, []))
    hub_full["config_dir"] = hub["config_dir"]
    hub_full["data_dir"] = hub["data_dir"]
    _campaign(hub_full, repo, "HUB-C", "QUEUED", project="Shared")

    def _host_cfg(tag):
        sub = tmp_path / tag
        sub.mkdir()
        return make_cfg(sub, repo, script(sub, []))

    host_a = _host_cfg("ha")
    host_b = _host_cfg("hb")
    _campaign(host_a, repo, "A-C", "QUEUED", project="Both")
    _campaign(host_b, repo, "B-C", "QUEUED", project="Both")
    _campaign(host_a, repo, "A2", "QUEUED", project="OnlyA")
    _campaign(host_a, repo, "SA", "QUEUED", project="Shared")
    _add_and_enrol(host_a, hub_full, monkeypatch, name="zlast")
    _add_and_enrol(host_b, hub_full, monkeypatch, name="mfirst")
    srv_a, url_a = _serve(host_a)
    srv_b, url_b = _serve(host_b)
    _point_hub(hub_full, "zlast", url_a)
    _point_hub(hub_full, "mfirst", url_b)
    r_overlap = _invoke(hub_full, monkeypatch, "check")
    assert r_overlap.exit_code == 1
    assert "project Both on mfirst and zlast" in r_overlap.output
    assert "project Shared on hub and zlast" in r_overlap.output
    doc_a = FH.status_document(host_a)
    rec_z = H.find(H.load(hub_full), "zlast")
    cache = rec_z["status_cache"]
    assert cache
    assert cache["fetched_at"] == fetched_at
    assert cache["projects"] == doc_a["projects"]
    assert cache["campaigns"] == doc_a["campaigns"]
    srv_a.shutdown()
    srv_a.server_close()
    srv_b.shutdown()
    srv_b.server_close()

    hub_disjoint = _cfg(tmp_path, "hub-disjoint")
    dis_root = tmp_path / "hub-disjoint-data"
    dis_root.mkdir()
    hub_disjoint_data = make_cfg(dis_root, repo, script(dis_root, []))
    hub_disjoint["data_dir"] = hub_disjoint_data["data_dir"]
    host_x = _host_cfg("hx")
    host_y = _host_cfg("hy")
    _campaign(host_x, repo, "X1", "QUEUED", project="Px")
    _campaign(host_y, repo, "Y1", "QUEUED", project="Py")
    _add_and_enrol(host_x, hub_disjoint, monkeypatch, name="xhost")
    _add_and_enrol(host_y, hub_disjoint, monkeypatch, name="yhost")
    sx, ux = _serve(host_x)
    sy, uy = _serve(host_y)
    _point_hub(hub_disjoint, "xhost", ux)
    _point_hub(hub_disjoint, "yhost", uy)
    r_ok = _invoke(hub_disjoint, monkeypatch, "check")
    assert r_ok.exit_code == 0
    assert "xhost ok" in r_ok.output and "yhost ok" in r_ok.output
    doc_x = FH.status_document(host_x)
    rec_x = H.find(H.load(hub_disjoint), "xhost")
    assert rec_x["status_cache"]["projects"] == doc_x["projects"]
    assert rec_x["status_cache"]["campaigns"] == doc_x["campaigns"]
    assert rec_x["status_cache"]["fetched_at"]
    sx.shutdown()
    sy.shutdown()

    _invoke(hub_full, monkeypatch, "off", "zlast")
    _invoke(hub_full, monkeypatch, "off", "mfirst")
    dead_port = _refused_port()
    _invoke(hub_full, monkeypatch, "add", "dead", "--url", f"http://127.0.0.1:{dead_port}")
    cache = {"fetched_at": "2020-01-01T00:00:00+00:00", "projects": ["Shared", "old"], "campaigns": []}

    def plant(recs):
        H.find(recs, "dead")["status_cache"] = cache

    H.update(hub_full, plant)
    snap = json.dumps(H.find(H.load(hub_full), "dead")["status_cache"], sort_keys=True)
    r_dead = _invoke(hub_full, monkeypatch, "check")
    assert r_dead.exit_code == 1 and "dead unreachable" in r_dead.output
    assert "project Shared on hub and dead" not in r_dead.output
    assert json.dumps(H.find(H.load(hub_full), "dead")["status_cache"], sort_keys=True) == snap

    _invoke(hub_full, monkeypatch, "off", "mfirst")
    _invoke(hub_full, monkeypatch, "off", "dead")

    def plant_disabled(recs):
        H.find(recs, "zlast")["enabled"] = False
        H.find(recs, "zlast")["status_cache"] = {
            "fetched_at": "x", "projects": ["Shared"], "campaigns": [],
        }

    H.update(hub_full, plant_disabled)
    contacted = []
    real_init = F.HubClient.__init__
    real_req = F.HubClient.request

    def spy_init(self, record, *a, **k):
        contacted.append(record.get("name"))
        return real_init(self, record, *a, **k)

    def spy_req(self, *a, **k):
        contacted.append(self.record.get("name"))
        return real_req(self, *a, **k)

    monkeypatch.setattr(F.HubClient, "__init__", spy_init)
    monkeypatch.setattr(F.HubClient, "request", spy_req)
    r_off = _invoke(hub_full, monkeypatch, "check")
    assert r_off.exit_code == 0
    assert "zlast off" in r_off.output
    assert "mfirst off" in r_off.output and "dead off" in r_off.output
    assert contacted == []


def test_hubclient_getaddrinfo_blocks_within_connect_budget(tmp_path, monkeypatch):
    import time

    monkeypatch.setattr(time, "time", lambda: NOW)
    hub = _cfg(tmp_path, "hub")
    host = _cfg(tmp_path, "host")
    blob = _add_and_enrol(host, hub, monkeypatch)
    srv, url = _serve(host)
    _invoke(hub, monkeypatch, "add", "wedged", "--url", "http://dns-wedged.test:1")
    _point_hub(hub, "box", url)
    block = threading.Event()
    real_gai = socket.getaddrinfo

    def gai(host, port, *a, **kw):
        if host == "dns-wedged.test":
            block.wait(10)
        return real_gai(host, port, *a, **kw)

    monkeypatch.setattr(socket, "getaddrinfo", gai)
    wedged = H.find(H.load(hub), "wedged")
    monkeypatch.setattr(F.HubClient, "CONNECT_TIMEOUT", 0.5)  # scaled budget; the block lasts 10 s
    t0 = time.monotonic()
    with pytest.raises(ConnectionError):
        F.HubClient(wedged, hub, now=NOW).request("GET", "/v1/status")
    assert time.monotonic() - t0 < 8.0  # far below the 10 s block, with room for a loaded runner
    block.set()
    cache = {"fetched_at": "2020-01-01T00:00:00+00:00", "projects": ["old"], "campaigns": []}

    def plant(recs):
        H.find(recs, "wedged")["status_cache"] = cache

    H.update(hub, plant)
    snap = json.dumps(H.find(H.load(hub), "wedged")["status_cache"], sort_keys=True)
    r = _invoke(hub, monkeypatch, "check")
    assert r.exit_code == 1
    assert "wedged unreachable" in r.output
    assert "box ok" in r.output
    assert json.dumps(H.find(H.load(hub), "wedged")["status_cache"], sort_keys=True) == snap
    srv.shutdown()
    srv.server_close()


def test_hubclient_tries_later_address_when_first_blackholes(tmp_path, monkeypatch):
    import time

    monkeypatch.setattr(time, "time", lambda: NOW)
    hub = _cfg(tmp_path, "hub")
    host = _cfg(tmp_path, "host")
    _add_and_enrol(host, hub, monkeypatch)
    srv, url = _serve(host)
    port = srv.server_address[1]
    dead = _refused_port()
    rec = {**H.find(H.load(hub), "box"), "url": f"http://dual-stack.test:{port}"}
    real_gai = socket.getaddrinfo

    def gai(host, pname, *a, **kw):
        if host == "dual-stack.test":
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", dead)),
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", port)),
            ]
        return real_gai(host, pname, *a, **kw)

    monkeypatch.setattr(socket, "getaddrinfo", gai)
    doc = F.HubClient(rec, hub, now=NOW).get_json("/v1/status")
    assert doc["schema"] == "nw.federation.status.v1"
    srv.shutdown()


def test_status_document_failure_returns_signed_error(tmp_path, monkeypatch):
    import time

    monkeypatch.setattr(time, "time", lambda: NOW)
    host = _cfg(tmp_path, "host")
    F.write_federation(host, {"hub_name": "h", "host_name": "box", "token": "tok", "secret": "abc"})
    srv, base = _serve(host)
    real_sd = FH.status_document

    def boom(cfg):
        raise RuntimeError("queue wedged")

    monkeypatch.setattr(FH, "status_document", boom)
    st, hdrs, raw = _raw("GET", base, "/v1/status", "tok", "abc")
    assert st == 500
    assert json.loads(raw) == {"error": "internal_error"}
    _resp_sig("abc", "GET", "/v1/status", hdrs, raw, st)
    monkeypatch.setattr(FH, "status_document", real_sd)
    srv.shutdown()


def test_https_connect_failure_is_unreachable(tmp_path, monkeypatch):
    import time

    monkeypatch.setattr(time, "time", lambda: NOW)
    hub = _cfg(tmp_path)
    closed_port = _refused_port()
    _invoke(hub, monkeypatch, "add", "tlsdead", "--url", f"https://127.0.0.1:{closed_port}")
    rec = H.find(H.load(hub), "tlsdead")
    with pytest.raises(ConnectionError):
        F.HubClient(rec, hub, now=NOW).request("GET", "/v1/status")
    cache = {"fetched_at": "2020-01-01T00:00:00+00:00", "projects": ["stale"], "campaigns": []}

    def plant(recs):
        H.find(recs, "tlsdead")["status_cache"] = cache

    H.update(hub, plant)
    snap = json.dumps(H.find(H.load(hub), "tlsdead")["status_cache"], sort_keys=True)
    r = _invoke(hub, monkeypatch, "check")
    assert r.exit_code == 1
    assert "tlsdead unreachable:" in r.output
    assert "Traceback" not in r.output
    assert json.dumps(H.find(H.load(hub), "tlsdead")["status_cache"], sort_keys=True) == snap


def test_hub_modules_do_not_import_queue():
    root = Path(__file__).resolve().parents[1] / "src" / "nightwright" / "agentic"
    for name in ("hosts.py", "federation.py", "cli_hosts.py"):
        text = (root / name).read_text()
        assert not _queue_import_violations(text), name
    fh_text = (root / "federation_host.py").read_text()
    assert not _queue_import_violations(fh_text, federation_host_payload_only=True)
    assert _queue_import_violations("from . import queue as Q\n\ndef f():\n    Q.park()\n")
    assert _queue_import_violations("import nightwright.agentic.queue\n")
    # payload-only mode (used for federation_host.py) flags any other queue call, allows payload
    assert _queue_import_violations("from . import queue as Q\ndef f():\n    Q.park()\n",
                                    federation_host_payload_only=True)
    assert not _queue_import_violations("from . import queue as Q\ndef f(cfg):\n    Q.queue_payload(cfg)\n",
                                        federation_host_payload_only=True)
    assert _queue_import_violations("from nightwright.agentic.queue import park\n")
    assert "subprocess" not in fh_text


def test_hubclient_get_json_recursion_error(tmp_path, monkeypatch):
    import time

    monkeypatch.setattr(time, "time", lambda: NOW)
    hub = _cfg(tmp_path, "hub")
    host = _cfg(tmp_path, "host")
    _add_and_enrol(host, hub, monkeypatch)
    srv, url = _serve(host)
    _point_hub(hub, "box", url)
    client = F.HubClient(H.load(hub)[0], hub, now=NOW)
    real_loads = json.loads

    def selective_loads(raw):
        text = raw if isinstance(raw, str) else raw.decode()
        if "nw.federation.status" in text:
            raise RecursionError("nested json")
        return real_loads(raw)

    monkeypatch.setattr(F.json, "loads", selective_loads)
    with pytest.raises(ConnectionError, match="not JSON"):
        client.get_json("/v1/status")
    srv.shutdown()


def test_inert_without_registry_files(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    payload = Q.queue_payload(cfg)
    assert payload["schema"] == "nw.queue.v1"
    assert not H.path(cfg).exists() and not F.federation_path(cfg).exists()
    assert _invoke(cfg, monkeypatch, "list").exit_code == 0
    assert not H.path(cfg).exists()


# --- loop 3 review hardening -------------------------------------------------------------------


def _raw_listener(handler):
    """A raw TCP listener on 127.0.0.1:0; `handler(conn)` runs per connection in a daemon thread.
    Returns (port, stop) where stop() closes the listener."""
    ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ls.bind(("127.0.0.1", 0))
    ls.listen(8)
    done = threading.Event()

    def loop():
        ls.settimeout(0.1)
        while not done.is_set():
            try:
                conn, _ = ls.accept()
            except OSError:
                continue
            threading.Thread(target=handler, args=(conn,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()

    def stop():
        done.set()
        ls.close()

    return ls.getsockname()[1], stop


def _hung(conn):
    conn.settimeout(0.1)
    try:
        for _ in range(60):
            try:
                conn.recv(4096)
            except socket.timeout:
                pass
            except OSError:
                return
    finally:
        conn.close()


def _drip(payload: bytes, gap: float):
    def handler(conn):
        try:
            conn.recv(65536)
            for i in range(len(payload)):
                conn.sendall(payload[i:i + 1])
                threading.Event().wait(gap)
        except OSError:
            pass
        finally:
            conn.close()

    return handler


def _client_for(hub, monkeypatch, port, name="slow"):
    r = _invoke(hub, monkeypatch, "add", name, "--url", f"http://127.0.0.1:{port}")
    assert r.exit_code == 0, r.output
    return F.HubClient(H.find(H.load(hub), name), hub, now=NOW)


def test_hubclient_hung_and_dripping_peers_are_cut_off(tmp_path, monkeypatch):
    import time

    hub = _cfg(tmp_path, "hub")
    monkeypatch.setattr(F.HubClient, "READ_TIMEOUT", 0.6)
    head = b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n"
    for label, handler in (
        ("hung", _hung),
        ("status-line drip", _drip(head, 0.25)),
        ("body drip", _drip(head + b"x" * 100, 0.25)),
    ):
        port, stop = _raw_listener(handler)
        client = _client_for(hub, monkeypatch, port, name="s" + label.replace(" ", "-")[:8])
        t0 = time.monotonic()
        with pytest.raises(ConnectionError):
            client.request("GET", "/v1/status")
        elapsed = time.monotonic() - t0
        stop()
        assert 0.4 <= elapsed < 8.0, (label, elapsed)


def test_hubclient_one_byte_per_second_drip_stops_near_two_seconds(tmp_path, monkeypatch):
    import time

    hub = _cfg(tmp_path, "hub")
    head = b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n"
    port, stop = _raw_listener(_drip(head + b"y" * 100, 1.0))
    monkeypatch.setattr(F.HubClient, "READ_TIMEOUT", 0.6)  # scaled budget; 100 B would take 100 s
    client = _client_for(hub, monkeypatch, port)
    t0 = time.monotonic()
    with pytest.raises(ConnectionError, match="timed out"):
        client.request("GET", "/v1/status")
    elapsed = time.monotonic() - t0
    stop()
    assert elapsed < 20.0


def _blocking_connect(monkeypatch, blocked_ports, release):
    real = socket.socket.connect

    def conn(self, addr):
        if addr[1] in blocked_ports:
            release.wait(10)
            raise OSError("blackholed")
        return real(self, addr)

    monkeypatch.setattr(socket.socket, "connect", conn)


def _dual(monkeypatch, ports):
    real_gai = socket.getaddrinfo

    def gai(host, pname, *a, **kw):
        if host == "dual-stack.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", p)) for p in ports]
        return real_gai(host, pname, *a, **kw)

    monkeypatch.setattr(socket, "getaddrinfo", gai)


def test_hubclient_blackholed_first_address_does_not_eat_the_budget(tmp_path, monkeypatch):
    import time

    monkeypatch.setattr(time, "time", lambda: NOW)
    hub = _cfg(tmp_path, "hub")
    host = _cfg(tmp_path, "host")
    _add_and_enrol(host, hub, monkeypatch)
    srv, _ = _serve(host)
    port = srv.server_address[1]
    hole = 1  # never dialled: connect() is replaced for this port
    release = threading.Event()
    _blocking_connect(monkeypatch, {hole}, release)
    _dual(monkeypatch, [hole, port])
    rec = {**H.find(H.load(hub), "box"), "url": f"http://dual-stack.test:{port}"}
    t0 = time.monotonic()
    doc = F.HubClient(rec, hub, now=NOW).get_json("/v1/status")
    assert doc["schema"] == "nw.federation.status.v1"
    assert time.monotonic() - t0 < 8.0  # the hole is held for 10 s; the second address wins
    # every address blackholed: bounded by the connect budget, then ConnectionError
    _dual(monkeypatch, [hole, hole + 1])
    _blocking_connect(monkeypatch, {hole, hole + 1}, release)
    monkeypatch.setattr(F.HubClient, "CONNECT_TIMEOUT", 0.5)
    t0 = time.monotonic()
    with pytest.raises(ConnectionError):
        F.HubClient(rec, hub, now=NOW).request("GET", "/v1/status")
    assert time.monotonic() - t0 < 8.0
    release.set()
    srv.shutdown()
    srv.server_close()


def test_hubclient_all_addresses_refused_fails_fast_even_if_a_socket_cannot_be_made(
        tmp_path, monkeypatch):
    import time

    hub = _cfg(tmp_path, "hub")
    _invoke(hub, monkeypatch, "add", "box", "--url", "http://dual-stack.test:1")
    rec = H.find(H.load(hub), "box")
    real_gai = socket.getaddrinfo
    dead = _refused_port()

    def gai(host, pname, *a, **kw):
        if host == "dual-stack.test":
            return [(socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("::1", dead, 0, 0)),
                    (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", dead))]
        return real_gai(host, pname, *a, **kw)

    real_cls = socket.socket

    class NoV6(real_cls):
        def __init__(self, family=-1, *a, **kw):
            if family == socket.AF_INET6:
                raise OSError(97, "Address family not supported by protocol")
            super().__init__(family, *a, **kw)

    uncaught: list = []
    monkeypatch.setattr(threading, "excepthook", lambda a: uncaught.append(a))
    monkeypatch.setattr(socket, "getaddrinfo", gai)
    monkeypatch.setattr(socket, "socket", NoV6)
    monkeypatch.setattr(F.HubClient, "CONNECT_TIMEOUT", 30)  # failing fast means not waiting this out
    t0 = time.monotonic()
    with pytest.raises(ConnectionError):
        F.HubClient(rec, hub, now=NOW).request("GET", "/v1/status")
    assert time.monotonic() - t0 < 15
    assert not uncaught


def test_https_serving_signed_200_verification_and_garbage_connection(tmp_path, monkeypatch):
    import ssl
    import time

    monkeypatch.setattr(time, "time", lambda: NOW)
    hub = _cfg(tmp_path, "hub")
    host = _cfg(tmp_path, "host")
    cert, key = _mini_tls_pair(tmp_path)
    _add_and_enrol(host, hub, monkeypatch)
    srv = FH.make_federate_server(host, "127.0.0.1:0", tls_cert=cert, tls_key=key)
    th = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    th.start()
    port = srv.server_address[1]
    _point_hub(hub, "box", f"https://127.0.0.1:{port}")
    rec = H.find(H.load(hub), "box")
    # a client that does not trust the self-signed certificate must refuse it
    with pytest.raises(ConnectionError, match="SSL|certificate|CERTIFICATE"):
        F.HubClient(rec, hub, now=NOW).request("GET", "/v1/status")
    # plain TCP junk on the TLS port, and a silent connection, do not wedge the next request
    junk = socket.create_connection(("127.0.0.1", port), timeout=2)
    junk.sendall(b"GET / HTTP/1.0\r\n\r\n")
    junk.close()
    silent = socket.create_connection(("127.0.0.1", port), timeout=2)
    real_ctx = ssl.create_default_context
    monkeypatch.setattr(ssl, "create_default_context", lambda *a, **k: real_ctx(cafile=cert))
    status, raw, hdrs = F.HubClient(rec, hub, now=NOW).request("GET", "/v1/status")
    assert status == 200
    assert json.loads(raw)["schema"] == "nw.federation.status.v1"
    assert any(k.lower() == "x-nw-sig" for k in hdrs)
    silent.close()
    srv.shutdown()
    srv.server_close()


def test_hubclient_requests_exact_target_for_trailing_slash_and_prefix(tmp_path, monkeypatch):
    import time

    monkeypatch.setattr(time, "time", lambda: NOW)
    hub = _cfg(tmp_path, "hub")
    host = _cfg(tmp_path, "host")
    _add_and_enrol(host, hub, monkeypatch)
    srv, url = _serve(host)
    seen: list = []
    real_request = http.client.HTTPConnection.request

    def spy(self, method, target, *a, **kw):
        seen.append(target)
        return real_request(self, method, target, *a, **kw)

    monkeypatch.setattr(http.client.HTTPConnection, "request", spy)
    rec = H.find(H.load(hub), "box")
    F.HubClient({**rec, "url": url + "/"}, hub, now=NOW).get_json("/v1/status")
    with pytest.raises(ConnectionError, match="HTTP 404"):
        F.HubClient({**rec, "url": url + "/pfx/"}, hub, now=NOW).get_json("/v1/status")
    assert seen == ["/v1/status", "/pfx/v1/status"]
    srv.shutdown()
    srv.server_close()


def test_add_accepts_path_prefix_verbatim_and_rolls_back_on_registry_failure(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    for name, url in (("pfx", "http://h.example:1/prefix"), ("slash", "http://H.example:2/")):
        assert _invoke(cfg, monkeypatch, "add", name, "--url", url).exit_code == 0
        assert H.find(H.load(cfg), name)["url"] == url
    listed = _invoke(cfg, monkeypatch, "list").output
    assert "pfx\thttp://h.example:1/prefix\ton" in listed
    assert "slash\thttp://H.example:2/\ton" in listed
    before = H.path(cfg).read_bytes()

    def boom(p, recs):
        raise OSError("disk full")

    monkeypatch.setattr(H, "_write_fsync", boom)
    r = _invoke(cfg, monkeypatch, "add", "fail", "--url", "http://h:3")
    assert r.exit_code != 0
    assert "Traceback" not in r.output
    assert not F.host_token_path(cfg, "fail").exists()
    assert not F.host_secret_path(cfg, "fail").exists()
    assert H.path(cfg).read_bytes() == before


def test_corrupt_registry_gives_one_line_errors_not_tracebacks(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    H.path(cfg).parent.mkdir(parents=True, exist_ok=True)
    H.path(cfg).write_text("{garbage")
    r = _invoke(cfg, monkeypatch, "add", "x", "--url", "http://h:1")
    assert r.exit_code != 0
    assert isinstance(r.exception, SystemExit)
    assert "Traceback" not in r.output
    assert H.path(cfg).read_text() == "{garbage"
    assert not F.host_token_path(cfg, "x").exists()
    r2 = _invoke(cfg, monkeypatch, "rm", "x")
    assert r2.exit_code != 0 and isinstance(r2.exception, SystemExit)


def test_secrets_and_federation_json_are_fsynced(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    synced: list = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real(fd))[1])
    F.write_host_secrets(cfg, "box", "tok", "sec")
    assert len(synced) == 2
    F.write_federation(cfg, {"hub_name": "h", "host_name": "b", "token": "t", "secret": "s"})
    assert len(synced) == 3
    assert stat.S_IMODE(F.federation_path(cfg).stat().st_mode) == 0o600


def test_update_where_fn_raises_keeps_previous_bytes(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    assert _invoke(cfg, monkeypatch, "add", "box", "--url", "http://h:1").exit_code == 0
    before = H.path(cfg).read_bytes()

    def fn(recs):
        recs.clear()
        raise RuntimeError("fn failed")

    with pytest.raises(RuntimeError):
        H.update(cfg, fn)
    assert H.path(cfg).read_bytes() == before
    assert not list(H.path(cfg).parent.glob(".hosts.json.*.tmp"))


def test_invalid_federation_json_under_live_server_is_unsigned_401(tmp_path, monkeypatch):
    import time

    monkeypatch.setattr(time, "time", lambda: NOW)
    hub = _cfg(tmp_path, "hub")
    host = _cfg(tmp_path, "host")
    blob = _add_and_enrol(host, hub, monkeypatch)
    srv, url = _serve(host)
    fp = F.federation_path(host)
    good = fp.read_text()
    assert _raw("GET", url, "/v1/status", blob["token"], blob["secret"])[0] == 200
    for bad in ("{not json", "[]", json.dumps({**json.loads(good), "secret": ""}),
                json.dumps({"hub_name": "h"})):
        fp.write_text(bad)
        status, hdrs, raw = _raw("GET", url, "/v1/status", blob["token"], blob["secret"])
        assert status == 401, bad
        assert not any(k.lower() in ("x-nw-sig", "x-nw-sig-ts") for k in hdrs), bad
        assert json.loads(raw) == {"error": "unauthorized"}
    fp.write_text(good)
    assert _raw("GET", url, "/v1/status", blob["token"], blob["secret"])[0] == 200
    srv.shutdown()
    srv.server_close()


def test_serve_federate_cli_refuses_public_binds_cleanly(tmp_path, monkeypatch):
    host = _cfg(tmp_path, "host")
    F.write_federation(host, {"hub_name": "h", "host_name": "b", "token": "t", "secret": "s"})
    monkeypatch.setattr("nightwright.cli.load_config", lambda: host)
    for bind in ("0.0.0.0:0", "[::]:0", "8.8.8.8:0"):
        r = CliRunner().invoke(cli, ["serve", "--federate", "--bind", bind])
        assert r.exit_code != 0, bind
        assert isinstance(r.exception, SystemExit), bind
        assert "Traceback" not in r.output

    def stop_now(self, *a, **kw):
        raise KeyboardInterrupt()

    monkeypatch.setattr(FH.FederateServer, "serve_forever", stop_now)
    r = CliRunner().invoke(cli, ["serve", "--federate", "--bind", "localhost:0"])
    assert r.exit_code == 0, r.output
    assert "nw serve: federation on" in r.stderr


def test_check_output_order_reasons_and_bad_documents(tmp_path, monkeypatch):
    import time

    monkeypatch.setattr(time, "time", lambda: NOW)
    hub = _cfg(tmp_path, "hub")
    for name in ("zlast", "badproj", "badschema", "mfirst", "unsigned"):
        assert _invoke(hub, monkeypatch, "add", name, "--url", "http://127.0.0.1:9").exit_code == 0
    # `unsigned` answers a real HTTP 200 with no signature at all
    body = b'{"schema":"nw.federation.status.v1","projects":[],"campaigns":[]}'
    reply = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
             + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)

    def answer(conn):
        try:
            conn.recv(65536)
            conn.sendall(reply)
        finally:
            conn.close()

    port, stop = _raw_listener(answer)
    _point_hub(hub, "unsigned", f"http://127.0.0.1:{port}")
    docs = {
        "zlast": {"schema": "nw.federation.status.v1", "projects": ["Z"], "campaigns": []},
        "mfirst": {"schema": "nw.federation.status.v1", "projects": ["Z"], "campaigns": []},
        "badproj": {"schema": "nw.federation.status.v1", "projects": 5, "campaigns": []},
        "badschema": {"schema": "other.v9", "projects": [], "campaigns": []},
    }
    real_get = F.HubClient.get_json
    monkeypatch.setattr(F.HubClient, "get_json",
                        lambda self, path: docs[self.record["name"]]
                        if self.record["name"] in docs else real_get(self, path))
    r = _invoke(hub, monkeypatch, "check")
    stop()
    assert r.exit_code == 1
    lines = r.output.splitlines()
    assert "Traceback" not in r.output
    assert lines[:5] == [
        "zlast ok",
        "badproj unreachable: bad status document",
        "badschema unreachable: bad schema",
        "mfirst ok",
        "unsigned unreachable: host uses an old signature version",
    ]
    assert lines[5:] == ["project Z on mfirst and zlast"]
    # the failed hosts' caches stay null, the ok ones are stored
    assert H.find(H.load(hub), "badproj")["status_cache"] is None
    assert H.find(H.load(hub), "zlast")["status_cache"]["projects"] == ["Z"]


def test_check_after_unenrol_reports_http_401(tmp_path, monkeypatch):
    import time

    monkeypatch.setattr(time, "time", lambda: NOW)
    hub = _cfg(tmp_path, "hub")
    host = _cfg(tmp_path, "host")
    _add_and_enrol(host, hub, monkeypatch)
    srv, url = _serve(host)
    _point_hub(hub, "box", url)
    assert _invoke(hub, monkeypatch, "check").output.splitlines()[0] == "box ok"
    F.federation_path(host).unlink()
    r = _invoke(hub, monkeypatch, "check")
    assert r.exit_code == 1
    assert r.output.splitlines()[0] == "box unreachable: host not enrolled"
    srv.shutdown()
    srv.server_close()


def _signed_reply(secret, *, ts_off=0, mac=None, body=None):
    body = body or b'{"schema":"nw.federation.status.v1","projects":[],"campaigns":[]}'
    ts = int(NOW) + ts_off
    sig = mac or F.sign(secret, "GET", "/v1/status", body, ts, direction="resp", status=200)
    head = (f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n"
            f"X-NW-Sig-Ts: {ts}\r\nX-NW-Sig: {sig}\r\nX-NW-Sig-V: 2\r\nConnection: close\r\n\r\n")
    return head.encode() + body


def test_check_signature_credential_and_cache_write_failures(tmp_path, monkeypatch):
    import time

    monkeypatch.setattr(time, "time", lambda: NOW)
    hub = _cfg(tmp_path, "hub")
    for name in ("badsig", "stale", "nocreds", "cachefail", "fresh"):
        assert _invoke(hub, monkeypatch, "add", name, "--url", "http://127.0.0.1:9").exit_code == 0

    def answering(reply):
        def answer(conn):
            try:
                conn.recv(65536)
                conn.sendall(reply)
            finally:
                conn.close()
        return answer

    stops = []
    for name, reply in (
        ("badsig", _signed_reply(F.read_host_secret(hub, "badsig"), mac="0" * 64)),
        ("stale", _signed_reply(F.read_host_secret(hub, "stale"), ts_off=-121)),
        ("cachefail", _signed_reply(F.read_host_secret(hub, "cachefail"))),
        ("fresh", _signed_reply(F.read_host_secret(hub, "fresh"), ts_off=-120)),
    ):
        port, stop = _raw_listener(answering(reply))
        stops.append(stop)
        _point_hub(hub, name, f"http://127.0.0.1:{port}")
    F.host_secret_path(hub, "nocreds").unlink()
    # a record with no name between real hosts: reported, and the run continues
    H.update(hub, lambda rs: rs.insert(1, {"url": "http://127.0.0.1:9", "enabled": True}))
    before = {r["name"]: r["status_cache"] for r in H.load(hub) if "name" in r}

    real_write = H._write_fsync
    calls = []

    def flaky_write(p, recs):
        # the first cache write is `cachefail`'s (earlier hosts all fail before storing)
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk full")
        return real_write(p, recs)

    # registry order: badsig, <malformed>, stale, nocreds, cachefail, fresh
    assert [r.get("name") for r in H.load(hub)] == [
        "badsig", None, "stale", "nocreds", "cachefail", "fresh"]
    monkeypatch.setattr(H, "_write_fsync", flaky_write)
    r = _invoke(hub, monkeypatch, "check")
    for stop in stops:
        stop()
    assert r.exit_code == 1
    assert "Traceback" not in r.output
    assert r.output.splitlines() == [
        "badsig unreachable: bad signature",
        "? unreachable: malformed host record",
        "stale unreachable: stale signature",
        "nocreds unreachable: missing host credentials",
        "cachefail unreachable: disk full",
        "fresh ok",
    ]
    monkeypatch.setattr(H, "_write_fsync", real_write)
    after = {r["name"]: r["status_cache"] for r in H.load(hub) if "name" in r}
    for name in ("badsig", "stale", "nocreds", "cachefail"):
        assert after[name] == before[name] is None
    assert after["fresh"]["projects"] == []


def test_check_caches_only_sanitised_campaigns(tmp_path, monkeypatch):
    hub = _cfg(tmp_path, "hub")
    assert _invoke(hub, monkeypatch, "add", "box", "--url", "http://127.0.0.1:9").exit_code == 0
    deep: list = []
    node = deep
    for _ in range(5000):
        nxt: list = []
        node.append(nxt)
        node = nxt
    good = {"campaign": "C1", "project": "P", "state": "QUEUED", "spec_id": "SPEC-1",
            "kind": "implement", "extra": "dropped"}
    doc = {"schema": "nw.federation.status.v1",
           "projects": ["P", "\x1b[31mevil", "x" * 1000, 7],
           "campaigns": [good, deep, "str", {"campaign": "\x1b]0;t\x07", "project": deep}]}
    monkeypatch.setattr(F.HubClient, "get_json", lambda self, path: doc)
    r = _invoke(hub, monkeypatch, "check")
    assert r.exit_code == 0, r.output
    assert r.output.splitlines() == ["box ok"]
    assert "\x1b" not in r.output
    cache = H.find(H.load(hub), "box")["status_cache"]
    assert cache["projects"] == ["P"]
    assert cache["campaigns"] == [
        {"campaign": "C1", "project": "P", "state": "QUEUED", "spec_id": "SPEC-1", "kind": "implement"},
        {"campaign": None, "project": None, "state": None, "spec_id": None, "kind": None},
    ]

    big = {"schema": "nw.federation.status.v1", "projects": [],
           "campaigns": [good] * 5000}
    monkeypatch.setattr(F.HubClient, "get_json", lambda self, path: big)
    assert _invoke(hub, monkeypatch, "check").exit_code == 0
    assert len(H.find(H.load(hub), "box")["status_cache"]["campaigns"]) == cli_hosts._MAX_CAMPAIGNS


def test_check_on_off_with_corrupt_registry_records(tmp_path, monkeypatch):
    hub = _cfg(tmp_path, "hub")
    host = _cfg(tmp_path, "host")
    _add_and_enrol(host, hub, monkeypatch)
    srv, url = _serve(host)
    _point_hub(hub, "box", url)
    good = H.find(H.load(hub), "box")
    p = H.path(hub)

    # unparseable and wrong-schema registries: check refuses, it does not report success
    for text in ("{garbage", json.dumps({"schema": "other", "hosts": [good]}),
                 json.dumps({"schema": H.SCHEMA, "hosts": "nope"})):
        p.write_text(text)
        r = _invoke(hub, monkeypatch, "check")
        assert r.exit_code != 0 and isinstance(r.exception, SystemExit), text
        assert "Traceback" not in r.output
        assert p.read_text() == text

    # a non-dict entry next to a valid host: no traceback, the valid host is still processed
    p.write_text(json.dumps({"schema": H.SCHEMA, "hosts": ["junk", good]}))
    r = _invoke(hub, monkeypatch, "check")
    assert r.exit_code == 1 and isinstance(r.exception, SystemExit)
    assert r.output.splitlines() == ["? unreachable: malformed host record", "box ok"]
    assert json.loads(p.read_text())["hosts"][0] == "junk"
    assert json.loads(p.read_text())["hosts"][1]["status_cache"]["projects"] == []
    r = _invoke(hub, monkeypatch, "off", "box")
    assert r.exit_code == 0 and r.exception is None, r.output
    assert json.loads(p.read_text())["hosts"][1]["enabled"] is False
    r = _invoke(hub, monkeypatch, "on", "box")
    assert r.exit_code == 0 and json.loads(p.read_text())["hosts"][1]["enabled"] is True
    for verb in ("on", "off"):
        r = _invoke(hub, monkeypatch, verb, "ghost")
        assert r.exit_code != 0 and isinstance(r.exception, SystemExit)
        assert "Traceback" not in r.output
    srv.shutdown()
    srv.server_close()


def test_enrol_deeply_nested_json_is_a_one_line_error(tmp_path):
    cfg = _cfg(tmp_path)
    env = {**os.environ, "NW_DATA_DIR": cfg["data_dir"], "NW_CONFIG_DIR": cfg["config_dir"]}
    r = CliRunner().invoke(cli, ["hosts", "enrol"], input="[" * 100000 + "\n", env=env)
    assert r.exit_code != 0
    assert not isinstance(r.exception, RecursionError)
    assert "not JSON" in r.output
    assert not F.federation_path(cfg).exists()


def test_secret_directories_are_0700_under_a_permissive_umask(tmp_path):
    cfg = _cfg(tmp_path)
    old = os.umask(0)
    try:
        F.write_host_secrets(cfg, "box", "tok", "sec")
        F.write_federation(cfg, {"hub_name": "h", "host_name": "b", "token": "t", "secret": "s"})
    finally:
        os.umask(old)
    root = Path(cfg["config_dir"]) / "secrets"
    for d in (root, root / "hosts"):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700, d
    assert stat.S_IMODE(F.host_token_path(cfg, "box").stat().st_mode) == 0o600


def test_serve_federate_refuses_dashboard_only_options(tmp_path, monkeypatch):
    host = _cfg(tmp_path, "host")
    F.write_federation(host, {"hub_name": "h", "host_name": "b", "token": "t", "secret": "s"})
    monkeypatch.setattr("nightwright.cli.load_config", lambda: host)

    def _no_server(*a, **kw):
        raise AssertionError("a server was constructed")

    monkeypatch.setattr(FH, "make_federate_server", _no_server)
    for extra in (["--code-root", str(tmp_path)], ["--no-code-watch"], ["--tailnet-wait", "5"]):
        r = CliRunner().invoke(cli, ["serve", "--federate", *extra])
        assert r.exit_code == 2, (extra, r.output)
        assert "--dashboard options" in r.output


def test_hubclient_closes_losing_sockets_of_a_multi_address_connect(tmp_path, monkeypatch):
    import time

    monkeypatch.setattr(time, "time", lambda: NOW)
    hub = _cfg(tmp_path, "hub")
    host = _cfg(tmp_path, "host")
    _add_and_enrol(host, hub, monkeypatch)
    srv, _ = _serve(host)
    port = srv.server_address[1]
    _dual(monkeypatch, [port, port, port])
    rec = {**H.find(H.load(hub), "box"), "url": f"http://dual-stack.test:{port}"}
    made: list = []
    real_cls = socket.socket

    class Tracked(real_cls):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            made.append(self)

    monkeypatch.setattr(socket, "socket", Tracked)
    doc = F.HubClient(rec, hub, now=NOW).get_json("/v1/status")
    assert doc["schema"] == "nw.federation.status.v1"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(s.fileno() != -1 for s in made):
        time.sleep(0.05)
    assert len(made) >= 3 and all(s.fileno() == -1 for s in made)
    srv.shutdown()
    srv.server_close()


def _replay_as_request(h, body=b""):
    """A signed response's own headers presented as the request's signature headers."""
    hdrs = {"Authorization": "Bearer tok", "X-NW-Req-Ts": h["X-NW-Sig-Ts"],
            "X-NW-Req-Sig": h["X-NW-Sig"], "X-NW-Sig-V": "2"}
    if body:
        hdrs["Content-Length"] = str(len(body))
    return hdrs


def test_signed_response_replayed_as_request_fails(tmp_path, monkeypatch):
    """SPEC-0595: a response MAC never verifies as a request MAC (GET, bodied POST, signed 401)."""
    import time

    monkeypatch.setattr(time, "time", lambda: NOW)
    host = _cfg(tmp_path, "host")
    secret, path, echo = "abc", "/v1/status", "/v1/_test_echo"
    F.write_federation(host, {"hub_name": "h", "host_name": "box", "token": "tok", "secret": secret})
    srv, base = _serve(host)
    srv.extra_routes[("POST", echo)] = lambda body: json.dumps({"echo": body.decode()}).encode()
    unrouted = FH.FederateServer(host, ("127.0.0.1", 0))
    assert unrouted.extra_routes == {}  # the seam ships no route
    unrouted.server_close()

    # GET (the 0529 case): the signed 200 replayed against the same bodyless request
    st, h, raw200 = _raw("GET", base, path, "tok", secret)
    assert st == 200
    _resp_sig(secret, "GET", path, h, raw200, st)
    assert F.verify(secret, "GET", path, raw200, h["X-NW-Sig"], int(h["X-NW-Sig-Ts"]),
                    direction="req", now=NOW) == "bad signature"
    st, h2, raw2 = _raw("GET", base, path, "tok", secret, headers=_replay_as_request(h))
    assert st == 401 and json.loads(raw2) == {"error": "unauthorized"}
    _resp_sig(secret, "GET", path, h2, raw2, st)
    # ... and with the signed body riding along (refused before the MAC check)
    st, _, _ = _raw("GET", base, path, "tok", secret, raw200, headers=_replay_as_request(h, raw200))
    assert st == 401

    # bodied POST to the test-only route: the signed 200 body R replayed as `POST echo` with body R
    body = b'{"hello":"world"}'
    st, h, raw_r = _raw("POST", base, echo, "tok", secret, body, headers={
        "Authorization": "Bearer tok", **F.request_headers(secret, "POST", echo, body, now=NOW),
        "Content-Length": str(len(body))})
    assert st == 200 and json.loads(raw_r) == {"echo": body.decode()}
    _resp_sig(secret, "POST", echo, h, raw_r, st)
    assert F.verify(secret, "POST", echo, raw_r, h["X-NW-Sig"], int(h["X-NW-Sig-Ts"]),
                    direction="req", now=NOW) == "bad signature"
    st, h3, raw3 = _raw("POST", base, echo, "tok", secret, raw_r, headers=_replay_as_request(h, raw_r))
    assert st == 401 and json.loads(raw3) == {"error": "unauthorized"}
    _resp_sig(secret, "POST", echo, h3, raw3, st)

    # a signed 401 (bad token) replayed as a bodied POST request
    st, h4, raw401 = _raw("POST", base, echo, "wrong", secret, body)
    assert st == 401
    _resp_sig(secret, "POST", echo, h4, raw401, st)
    st, h5, raw5 = _raw("POST", base, echo, "tok", secret, raw401,
                        headers=_replay_as_request(h4, raw401))
    assert st == 401 and json.loads(raw5) == {"error": "unauthorized"}
    assert F.verify(secret, "POST", echo, raw401, h4["X-NW-Sig"], int(h4["X-NW-Sig-Ts"]),
                    direction="req", now=NOW) == "bad signature"
    # the reverse: a request MAC is no response MAC
    rq = F.request_headers(secret, "GET", path, b"", now=NOW)
    assert F.verify(secret, "GET", path, b"", rq["X-NW-Req-Sig"], int(rq["X-NW-Req-Ts"]),
                    direction="resp", status=200, now=NOW) == "bad signature"
    srv.shutdown()


def test_response_mac_binds_the_status():
    ts = 1700000000
    for s_ in (200, 401, 404, 503):
        mac = F.sign("abc", "GET", "/v1/status", b"x", ts, direction="resp", status=s_)
        assert F.verify("abc", "GET", "/v1/status", b"x", mac, ts, direction="resp", status=s_,
                        now=NOW) is None
        for t_ in (200, 401, 404, 503):
            if t_ != s_:
                assert F.verify("abc", "GET", "/v1/status", b"x", mac, ts, direction="resp",
                                status=t_, now=NOW) == "bad signature"


def test_host_demands_the_sig_version_marker(tmp_path, monkeypatch):
    import time

    monkeypatch.setattr(time, "time", lambda: NOW)
    host = _cfg(tmp_path, "host")
    secret, path = "abc", "/v1/status"
    F.write_federation(host, {"hub_name": "h", "host_name": "box", "token": "tok", "secret": secret})
    srv, base = _serve(host)
    good = {"Authorization": "Bearer tok", **F.request_headers(secret, "GET", path, b"", now=NOW)}
    for label, hdrs in {
        "missing": {k: v for k, v in good.items() if k != "X-NW-Sig-V"},
        "v1": {**good, "X-NW-Sig-V": "1"},
        "empty": {**good, "X-NW-Sig-V": ""},
        "padded": {**good, "X-NW-Sig-V": "02"},
    }.items():
        st, h, raw = _raw("GET", base, path, "tok", secret, headers=hdrs)
        assert st == 401 and json.loads(raw) == {"error": "sig_version"}, label
        _resp_sig(secret, "GET", path, h, raw, st)
    # the bearer-token check still comes first
    st, h, raw = _raw("GET", base, path, "wrong", secret,
                      headers={**good, "Authorization": "Bearer wrong", "X-NW-Sig-V": "1"})
    assert st == 401 and json.loads(raw) == {"error": "unauthorized"}
    # a lower-case header name is accepted
    low = {k.lower() if k == "X-NW-Sig-V" else k: v for k, v in good.items()}
    st, _, _ = _raw("GET", base, path, "tok", secret, headers=low)
    assert st == 200
    # the marker alone is not auth: an old four-field MAC under a v2 marker is a bad MAC
    import hashlib
    import hmac as _hmac
    ts = int(NOW)
    old = _hmac.new(secret.encode(), f"GET|{path}|{F.EMPTY_BODY_SHA}|{ts}".encode(),
                    hashlib.sha256).hexdigest()
    st, h, raw = _raw("GET", base, path, "tok", secret, headers={**good, "X-NW-Req-Sig": old})
    assert st == 401 and json.loads(raw) == {"error": "unauthorized"}
    _resp_sig(secret, "GET", path, h, raw, st)
    srv.shutdown()


def _scripted_host(monkeypatch, tmp_path):
    """A hub whose one host `box` is a raw listener that replies whatever `reply[0]` holds;
    returns (client, secret, reply, stop)."""
    hub = _cfg(tmp_path, "hub")
    host = _cfg(tmp_path, "host")
    blob = _add_and_enrol(host, hub, monkeypatch)
    reply = [b""]

    def answer(conn):
        try:
            conn.recv(65536)
            conn.sendall(reply[0])
        finally:
            conn.close()

    port, stop = _raw_listener(answer)
    _point_hub(hub, "box", f"http://127.0.0.1:{port}")
    return F.HubClient(H.find(H.load(hub), "box"), hub, now=NOW), blob["secret"], reply, stop


def _wire(status, body, *, extra=()):
    head = f"HTTP/1.1 {status} X\r\nContent-Length: {len(body)}\r\nConnection: close\r\n"
    head += "".join(f"{k}: {v}\r\n" for k, v in extra)
    return head.encode() + b"\r\n" + body


def _signed(secret, status, body, *, sign_status=None, marker="2", ts_off=0, mac=None):
    ts = int(NOW) + ts_off
    sig = mac or F.sign(secret, "GET", "/v1/status", body, ts, direction="resp",
                        status=status if sign_status is None else sign_status)
    extra = [("X-NW-Sig-Ts", ts), ("X-NW-Sig", sig)]
    if marker is not None:
        extra.append(("X-NW-Sig-V", marker))
    return _wire(status, body, extra=extra)


def test_hubclient_check_order(tmp_path, monkeypatch):
    client, secret, reply, stop = _scripted_host(monkeypatch, tmp_path)
    unauth = b'{"error":"unauthorized"}'
    doc = b'{"schema":"nw.federation.status.v1","projects":[],"campaigns":[]}'

    def err(wire):
        reply[0] = wire
        with pytest.raises(ConnectionError) as ei:
            client.request("GET", "/v1/status")
        return str(ei.value)

    # 1. the unenrolled host's unsigned 401
    assert err(_wire(401, unauth)) == "host not enrolled"
    # 2. no marker / wrong marker, whatever the status
    assert err(_wire(401, b'{"error":"other"}')) == "host uses an old signature version"
    assert err(_wire(404, b"{}")) == "host uses an old signature version"
    assert err(_signed(secret, 200, doc, marker=None)) == "host uses an old signature version"
    assert err(_signed(secret, 200, doc, marker="1")) == "host uses an old signature version"
    assert err(_signed(secret, 401, unauth, marker="1")) == "host uses an old signature version"
    # 3. signature failures, never a terminal HTTP 4xx
    assert err(_wire(404, b"{}", extra=[("X-NW-Sig-V", "2")])) == "missing response signature"
    assert err(_wire(403, unauth, extra=[("X-NW-Sig-V", "2")])) == "missing response signature"
    assert err(_signed(secret, 404, b"{}", mac="0" * 64)) == "bad signature"
    assert err(_signed(secret, 404, b"{}", ts_off=-500)) == "stale signature"
    assert err(_signed(secret, 200, doc, mac="0" * 64)) == "bad signature"
    # a signed 401 relabelled as 200 (and the reverse) fails the MAC
    assert err(_signed(secret, 200, unauth, sign_status=401)) == "bad signature"
    assert err(_signed(secret, 401, doc, sign_status=200)) == "bad signature"
    # a request-direction MAC on a reply is no response MAC
    req_mac = F.sign(secret, "GET", "/v1/status", doc, int(NOW), direction="req")
    assert err(_signed(secret, 200, doc, mac=req_mac)) == "bad signature"
    # 4. a verified non-200
    assert err(_signed(secret, 404, b"{}")) == "HTTP 404"
    assert err(_signed(secret, 401, b'{"error":"sig_version"}')) == "HTTP 401"
    assert err(_signed(secret, 401, unauth)) == "HTTP 401"
    # 5. a verified 200
    reply[0] = _signed(secret, 200, doc)
    st, raw, hdrs = client.request("GET", "/v1/status")
    assert st == 200 and raw == doc
    stop()
