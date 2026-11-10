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
         "-out", "c.pem", "-days", "1", "-subj", "/CN=localhost"],
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
    assert set(rec) == H.HOST_KEYS
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
    real = PJ.write_private
    calls = {"n": 0}

    def flaky(path, text):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("no space")
        return real(path, text)

    monkeypatch.setattr(PJ, "write_private", flaky)
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
    assert _invoke(cfg, monkeypatch, "add", "ok2", "--url", "http://127.0.0.1:8792/v1/status").exit_code != 0


def test_list_hides_secrets_and_missing_file_is_empty(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    assert _invoke(cfg, monkeypatch, "list").exit_code == 0
    assert _invoke(cfg, monkeypatch, "list").output == ""
    assert not H.path(cfg).exists()
    _invoke(cfg, monkeypatch, "add", "box", "--url", "http://127.0.0.1:1")
    out = _invoke(cfg, monkeypatch, "list").output.strip()
    assert out == "box\thttp://127.0.0.1:1\ton"
    assert "token" not in out


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
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    accepted = []
    stop = threading.Event()

    def accept_loop():
        srv.settimeout(0.25)
        while not stop.is_set():
            try:
                accepted.append(srv.accept())
            except OSError:
                pass

    _invoke(cfg, monkeypatch, "add", "box", "--url", f"http://127.0.0.1:{port}")
    th = threading.Thread(target=accept_loop, daemon=True)
    th.start()
    r = _invoke(cfg, monkeypatch, "rm", "box")
    stop.set()
    assert r.exit_code == 0
    assert "run nw hosts unenrol on the host" in r.output
    th.join(timeout=1)
    assert not accepted
    assert not F.host_token_path(cfg, "box").exists()
    assert not F.host_secret_path(cfg, "box").exists()
    assert not H.load(cfg)
    srv.close()
    r_unknown = _invoke(cfg, monkeypatch, "rm", "nobody")
    assert r_unknown.exit_code != 0
    assert "run nw hosts unenrol" not in r_unknown.output


def test_on_off_sets_enabled(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _invoke(cfg, monkeypatch, "add", "box", "--url", "http://127.0.0.1:1")
    snap = H.path(cfg).read_bytes()
    assert _invoke(cfg, monkeypatch, "off", "box").exit_code == 0
    assert H.load(cfg)[0]["enabled"] is False
    assert _invoke(cfg, monkeypatch, "list").output.strip() == "box\thttp://127.0.0.1:1\toff"
    assert _invoke(cfg, monkeypatch, "on", "box").exit_code == 0
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
    assert r.exit_code == 0
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


def test_hmac_test_vector():
    assert F.sign("abc", "GET", "/v1/status", b"", 1700000000) == (
        "53f715f892cd9e350af654bcacaca46fd6dcedb104c690ffaa9060fac03a49b5")
    assert F.verify("abc", "GET", "/v1/status", b"", None, 1700000000, now=NOW) == "bad signature"
    key_p = Path(__file__).resolve().parent
    cfg = {"config_dir": str(key_p), "data_dir": str(key_p)}
    sec_p = F.host_secret_path(cfg, "_nosuch")
    sec_p.parent.mkdir(parents=True, exist_ok=True)
    sec_p.write_text("abc \n")
    try:
        assert F.read_host_secret(cfg, "_nosuch") == "abc "
    finally:
        sec_p.unlink(missing_ok=True)


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
    real_cc = socket.create_connection

    def track_cc(addr, timeout=None, *a, **kw):
        connect_timeouts.append(timeout)
        sock = real_cc(addr, timeout=timeout, *a, **kw)
        client_socks.add(id(sock))
        return sock

    monkeypatch.setattr(socket, "create_connection", track_cc)
    real_settimeout = socket.socket.settimeout

    def track_st(self, timeout):
        if id(self) in client_socks:
            read_timeouts.append(timeout)
        return real_settimeout(self, timeout)

    monkeypatch.setattr(socket.socket, "settimeout", track_st)
    for _ in range(3):
        assert client.get_json("/v1/status")["schema"] == "nw.federation.status.v1"
    assert connect_timeouts == [2, 2, 2]
    assert read_timeouts[-3:] == [2, 2, 2]
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
    _campaign(host, repo, "SPEC-1", "QUEUED", project="P1")
    _campaign(host, repo, "SPEC-2", "ABANDONED", project="P2")
    F.write_federation(host, {"hub_name": "h", "host_name": "box", "token": "t", "secret": "abc"})
    srv, _ = _serve(host)
    payload = Q.queue_payload(host)
    doc = FH.status_document(host)
    assert doc["schema"] == "nw.federation.status.v1"
    want_items = [
        {k: item.get(k) for k in ("campaign", "project", "state", "spec_id", "kind")}
        for item in payload["items"]
    ]
    assert doc["campaigns"] == want_items
    assert doc["projects"] == sorted({i["project"] for i in want_items if i.get("project")})
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
    assert CliRunner().invoke(cli, ["serve", "--federate", "--client-ca", "/x"]).exit_code != 0
    assert CliRunner().invoke(cli, ["serve", "--jobs", "--federate"]).exit_code != 0
    cert, key = _mini_tls_pair(tmp_path)
    with pytest.raises(ValueError, match="tls-cert"):
        FH.make_federate_server(host, "127.0.0.1:0", tls_cert=cert, tls_key=None)
    with pytest.raises(ValueError, match="client-ca"):
        FH.make_federate_server(host, "127.0.0.1:0", client_ca=cert)
    FH.make_federate_server(host, "127.0.0.1:0", tls_cert=cert, tls_key=key).server_close()
    assert CliRunner().invoke(cli, ["serve", "--federate", "--tls-cert", cert]).exit_code != 0

    def puff(self, *a, **kw):
        raise KeyboardInterrupt()

    monkeypatch.setattr(FH.FederateServer, "serve_forever", puff)
    r = CliRunner().invoke(cli, ["serve", "--federate", "--bind", "127.0.0.1:0"])
    assert r.exit_code == 0
    assert "nw serve: federation on" in r.stderr
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
    _invoke(hub_full, monkeypatch, "add", "dead", "--url", "http://127.0.0.1:9")
    cache = {"fetched_at": "2020-01-01T00:00:00+00:00", "projects": ["old"], "campaigns": []}

    def plant(recs):
        H.find(recs, "dead")["status_cache"] = cache

    H.update(hub_full, plant)
    snap = json.dumps(H.find(H.load(hub_full), "dead")["status_cache"], sort_keys=True)
    r_dead = _invoke(hub_full, monkeypatch, "check")
    assert r_dead.exit_code == 1 and "dead unreachable" in r_dead.output
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


def test_https_connect_failure_is_unreachable(tmp_path, monkeypatch):
    import time

    monkeypatch.setattr(time, "time", lambda: NOW)
    hub = _cfg(tmp_path)
    closed_port = 59991
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


def test_inert_without_registry_files(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    payload = Q.queue_payload(cfg)
    assert payload["schema"] == "nw.queue.v1"
    assert not H.path(cfg).exists() and not F.federation_path(cfg).exists()
    assert _invoke(cfg, monkeypatch, "list").exit_code == 0
    assert not H.path(cfg).exists()
