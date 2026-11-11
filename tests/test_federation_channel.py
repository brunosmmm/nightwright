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


def _resp_sig(secret, method, path, hdrs, raw):
    ts = hdrs.get("X-NW-Sig-Ts") or hdrs.get("x-nw-sig-ts")
    mac = hdrs.get("X-NW-Sig") or hdrs.get("x-nw-sig")
    assert ts and mac
    assert F.verify(secret, method, path, raw, mac, int(ts), now=NOW) is None


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
        (["hosts", "enrol", "x"], None),
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


def test_hmac_test_vector(tmp_path):
    assert F.sign("abc", "GET", "/v1/status", b"", 1700000000) == (
        "53f715f892cd9e350af654bcacaca46fd6dcedb104c690ffaa9060fac03a49b5")
    assert F.verify("abc", "GET", "/v1/status", b"", None, 1700000000, now=NOW) == "bad signature"
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
    _resp_sig(secret, "GET", "/v1/status", h, raw)
    no_sig = {"Authorization": "Bearer tok", "X-NW-Req-Ts": str(int(NOW))}
    st, h, raw = req("GET", "/v1/status", headers=no_sig)
    assert st == 401
    _resp_sig(secret, "GET", "/v1/status", h, raw)
    no_ts = {**F.request_headers(secret, "GET", "/v1/status", b"", now=NOW),
             "Authorization": "Bearer tok"}
    del no_ts["X-NW-Req-Ts"]
    st, h, raw = req("GET", "/v1/status", headers=no_ts)
    assert st == 401
    _resp_sig(secret, "GET", "/v1/status", h, raw)
    hdrs = {**F.request_headers(secret, "GET", "/v1/status", b"", now=NOW), "Authorization": "Bearer tok"}
    st, h, raw = req("GET", "/v1/status", headers=hdrs)
    assert st == 200
    hdrs_bad = {**F.request_headers(secret, "GET", "/v1/status", b"", now=NOW), "Authorization": "Bearer wrong"}
    st, h, raw = req("GET", "/v1/status", headers=hdrs_bad)
    assert st == 401
    _resp_sig(secret, "GET", "/v1/status", h, raw)
    hdrs = {**F.request_headers(secret, "GET", "/v1/status", b"", now=NOW + 121), "Authorization": "Bearer tok"}
    st, h, raw = req("GET", "/v1/status", headers=hdrs)
    assert st == 401
    _resp_sig(secret, "GET", "/v1/status", h, raw)
    hdrs_past = {**F.request_headers(secret, "GET", "/v1/status", b"", now=NOW - 121),
                 "Authorization": "Bearer tok"}
    st, h, raw = req("GET", "/v1/status", headers=hdrs_past)
    assert st == 401
    _resp_sig(secret, "GET", "/v1/status", h, raw)
    hdrs = {**F.request_headers(secret, "GET", "/v1/status", b"", now=NOW + 120), "Authorization": "Bearer tok"}
    assert req("GET", "/v1/status", headers=hdrs)[0] == 200
    hdrs_p120 = {**F.request_headers(secret, "GET", "/v1/status", b"", now=NOW - 120),
                 "Authorization": "Bearer tok"}
    assert req("GET", "/v1/status", headers=hdrs_p120)[0] == 200
    body = b"x"
    hdrs = {**F.request_headers(secret, "GET", "/v1/status", body, now=NOW), "Authorization": "Bearer tok"}
    st, h, raw = req("GET", "/v1/status", body=body, headers={**hdrs, "Content-Length": "1"})
    assert st == 200
    hdrs_empty = {**F.request_headers(secret, "GET", "/v1/status", b"", now=NOW), "Authorization": "Bearer tok"}
    st, h, raw = req("GET", "/v1/status", body=body, headers={**hdrs_empty, "Content-Length": "1"})
    assert st == 401
    _resp_sig(secret, "GET", "/v1/status", h, raw)
    tampered = {**hdrs, "X-NW-Req-Sig": hdrs["X-NW-Req-Sig"][:-1] + ("0" if hdrs["X-NW-Req-Sig"][-1] != "0" else "1")}
    st, h, raw = req("GET", "/v1/status", headers=tampered)
    assert st == 401
    _resp_sig(secret, "GET", "/v1/status", h, raw)
    post_body = b"!"
    post_hdrs = {**F.request_headers(secret, "POST", "/v1/status", post_body, now=NOW), "Authorization": "Bearer tok",
                 "Content-Length": "1"}
    st, h, raw = req("POST", "/v1/status", body=b"?", headers=post_hdrs)
    assert st == 401
    _resp_sig(secret, "POST", "/v1/status", h, raw)
    post_hdrs = {**F.request_headers(secret, "POST", "/v1/status", b"", now=NOW), "Authorization": "Bearer tok"}
    st, h, raw = req("POST", "/v1/status", headers=post_hdrs)
    assert st == 405
    _resp_sig(secret, "POST", "/v1/status", h, raw)
    err405 = json.dumps({"error": "method_not_allowed"}, separators=(",", ":")).encode()
    for method in ("HEAD", "PATCH"):
        m_hdrs = {**F.request_headers(secret, method, "/v1/status", b"", now=NOW), "Authorization": "Bearer tok"}
        st, h, raw = req(method, "/v1/status", headers=m_hdrs)
        assert st == 405
        sig_body = b"" if method == "HEAD" else raw
        _resp_sig(secret, method, "/v1/status", h, sig_body)
    get_hdrs = {**F.request_headers(secret, "GET", "/nope", b"", now=NOW), "Authorization": "Bearer tok"}
    st, h, raw = req("GET", "/nope", headers=get_hdrs)
    assert st == 404
    _resp_sig(secret, "GET", "/nope", h, raw)
    patch_hdrs = {**F.request_headers(secret, "PATCH", "/nope", b"", now=NOW), "Authorization": "Bearer tok"}
    st, h, raw = req("PATCH", "/nope", headers=patch_hdrs)
    assert st == 404
    _resp_sig(secret, "PATCH", "/nope", h, raw)
    rev_hdrs = {**F.request_headers(secret, "POST", "/v1/revoke", b"", now=NOW), "Authorization": "Bearer tok"}
    st, h, raw = req("POST", "/v1/revoke", headers=rev_hdrs)
    assert st == 404
    _resp_sig(secret, "POST", "/v1/revoke", h, raw)
    qpath = "/v1/status?z=1&a=2"
    q_hdrs = {**F.request_headers(secret, "GET", qpath, b"", now=NOW), "Authorization": "Bearer tok"}
    st, h, raw = req("GET", qpath, headers=q_hdrs)
    assert st == 200
    _resp_sig(secret, "GET", qpath, h, raw)
    bad_q = {**F.request_headers(secret, "GET", "/v1/status?a=2&z=1", b"", now=NOW),
             "Authorization": "Bearer tok"}
    st, h, raw = req("GET", qpath, headers=bad_q)
    assert st == 401
    _resp_sig(secret, "GET", qpath, h, raw)
    unicode_ts = {**F.request_headers(secret, "GET", "/v1/status", b"", now=NOW),
                "Authorization": "Bearer tok", "X-NW-Req-Ts": "\u00b2"}
    st, h, raw = req("GET", "/v1/status", headers=unicode_ts)
    assert st == 401
    _resp_sig(secret, "GET", "/v1/status", h, raw)
    lz = {**F.request_headers(secret, "GET", "/v1/status", b"", now=NOW),
          "Authorization": "Bearer tok", "X-NW-Req-Ts": "01700000000"}
    st, h, raw = req("GET", "/v1/status", headers=lz)
    assert st == 401
    _resp_sig(secret, "GET", "/v1/status", h, raw)
    lo = {**F.request_headers(secret, "GET", "/v1/status", b"", now=NOW), "Authorization": "Bearer tok"}
    lo["x-nw-req-sig"] = lo.pop("X-NW-Req-Sig")
    lo["x-nw-req-ts"] = lo.pop("X-NW-Req-Ts")
    assert req("GET", "/v1/status", headers=lo)[0] == 200
    cl = {**F.request_headers(secret, "GET", "/v1/status", b"", now=NOW), "Authorization": "Bearer tok",
          "Content-Length": "\u00b2"}
    st, h, raw = req("GET", "/v1/status", headers=cl)
    assert st == 200
    _resp_sig(secret, "GET", "/v1/status", h, raw)
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
    _resp_sig(secret, "GET", path, h, raw)

    def flip(hdrs):
        sig = hdrs["X-NW-Req-Sig"]
        return {**hdrs, "X-NW-Req-Sig": ("0" if sig[0] != "0" else "1") + sig[1:]}

    def wrong_secret(hdrs):
        return {**hdrs, "X-NW-Req-Sig": F.sign("not-abc", "GET", path, b"", int(NOW))}

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
        _resp_sig(secret, "GET", path, h, raw)  # enrolled: the 401 is signed

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
                hdrs = F.response_headers(secret, "GET", "/v1/status", body, now=NOW)
                self.send_header("X-NW-Sig-Ts", hdrs["X-NW-Sig-Ts"])
                self.send_header("X-NW-Sig", hdrs["X-NW-Sig"])
                body = body[:20] + bytes([body[20] ^ 0x01]) + body[21:]
            elif bad_mode[0] == "skew121":
                hdrs = F.response_headers(secret, "GET", "/v1/status", body, now=NOW)
                self.send_header("X-NW-Sig-Ts", hdrs["X-NW-Sig-Ts"])
                self.send_header("X-NW-Sig", hdrs["X-NW-Sig"])
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
    _resp_sig("abc", "GET", "/v1/lease", lease_hdrs, lease_raw)
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
    srv_b.shutdown()

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
    r_off = _invoke(hub_full, monkeypatch, "check")
    assert r_off.exit_code == 0
    assert "zlast off" in r_off.output


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
    t0 = time.monotonic()
    with pytest.raises(ConnectionError):
        F.HubClient(wedged, hub, now=NOW).request("GET", "/v1/status")
    assert time.monotonic() - t0 <= 4.0
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
    _resp_sig("abc", "GET", "/v1/status", hdrs, raw)
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
        assert 0.4 <= elapsed <= 3.0, (label, elapsed)


def test_hubclient_one_byte_per_second_drip_stops_near_two_seconds(tmp_path, monkeypatch):
    import time

    hub = _cfg(tmp_path, "hub")
    head = b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n"
    port, stop = _raw_listener(_drip(head + b"y" * 100, 1.0))
    client = _client_for(hub, monkeypatch, port)
    assert client.READ_TIMEOUT == 2
    t0 = time.monotonic()
    with pytest.raises(ConnectionError, match="timed out"):
        client.request("GET", "/v1/status")
    elapsed = time.monotonic() - t0
    stop()
    assert elapsed <= 4.5


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
    assert time.monotonic() - t0 < 1.5
    # every address blackholed: bounded by the connect budget, then ConnectionError
    _dual(monkeypatch, [hole, hole + 1])
    _blocking_connect(monkeypatch, {hole, hole + 1}, release)
    monkeypatch.setattr(F.HubClient, "CONNECT_TIMEOUT", 0.5)
    t0 = time.monotonic()
    with pytest.raises(ConnectionError):
        F.HubClient(rec, hub, now=NOW).request("GET", "/v1/status")
    assert time.monotonic() - t0 <= 3.0
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
    t0 = time.monotonic()
    with pytest.raises(ConnectionError):
        F.HubClient(rec, hub, now=NOW).request("GET", "/v1/status")
    assert time.monotonic() - t0 < 1.5
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
        "unsigned unreachable: missing response signature",
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
    assert r.output.splitlines()[0] == "box unreachable: HTTP 401"
    srv.shutdown()
    srv.server_close()
