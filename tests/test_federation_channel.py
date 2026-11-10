"""SPEC-0529: federation enrolment and the signed host channel."""
from __future__ import annotations

import ast
import http.client
import json
import os
import socket
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

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


def _raw(method, url, path, token, secret, body=b"", *, now=NOW):
    host, port = url.split("://")[1].split(":")
    port = int(port)
    conn = http.client.HTTPConnection(host, port, timeout=5)
    hdrs = {"Authorization": f"Bearer {token}", **F.request_headers(secret, method, path, body, now=now)}
    conn.request(method, path, body=body, headers=hdrs)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, dict(resp.getheaders()), raw


def test_add_stores_0600_secrets_and_hash_only_record(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    r = _invoke(cfg, monkeypatch, "add", "box", "--url", "http://127.0.0.1:8792")
    assert r.exit_code == 0
    blob = json.loads(r.output.strip())
    assert set(blob) == {"hub_name", "host_name", "token", "secret"}
    assert blob["host_name"] == "box"
    tok_p = Path(cfg["config_dir"]) / "secrets/hosts/box.token"
    key_p = Path(cfg["config_dir"]) / "secrets/hosts/box.key"
    assert stat.S_IMODE(tok_p.stat().st_mode) == 0o600
    assert stat.S_IMODE(key_p.stat().st_mode) == 0o600
    assert stat.S_IMODE(tok_p.parent.stat().st_mode) == 0o700
    rec = H.load(cfg)[0]
    assert set(rec) == H.HOST_KEYS
    assert rec["secret_ref"] == "secrets/hosts/box.key"
    assert rec["token_sha256"] == PJ.token_hash(blob["token"])
    assert tok_p.read_text().strip() == blob["token"]


def test_add_rejects_duplicate_bad_name_and_bad_url(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    assert _invoke(cfg, monkeypatch, "add", "box", "--url", "http://127.0.0.1:8792").exit_code == 0
    assert _invoke(cfg, monkeypatch, "add", "box", "--url", "http://127.0.0.1:8792").exit_code != 0
    assert _invoke(cfg, monkeypatch, "add", "BAD", "--url", "http://x").exit_code != 0
    before = list(Path(cfg["config_dir"]).glob("secrets/hosts/*"))
    assert _invoke(cfg, monkeypatch, "add", "ok", "--url", "ftp://x").exit_code != 0
    assert list(Path(cfg["config_dir"]).glob("secrets/hosts/*")) == before


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


def test_rm_deletes_secrets_prints_unenrol_and_makes_no_http(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    accepted = []
    def accept():
        try:
            srv.settimeout(0.3)
            accepted.append(srv.accept())
        except OSError:
            pass
    th = threading.Thread(target=accept, daemon=True)
    th.start()
    _invoke(cfg, monkeypatch, "add", "box", "--url", f"http://127.0.0.1:{port}")
    r = _invoke(cfg, monkeypatch, "rm", "box")
    assert r.exit_code == 0
    assert "run nw hosts unenrol on the host" in r.output
    th.join(timeout=1)
    assert not accepted
    assert not F.host_token_path(cfg, "box").exists()
    assert not H.load(cfg)
    srv.close()


def test_on_off_sets_enabled(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _invoke(cfg, monkeypatch, "add", "box", "--url", "http://127.0.0.1:1")
    assert _invoke(cfg, monkeypatch, "off", "box").exit_code == 0
    assert H.load(cfg)[0]["enabled"] is False
    assert _invoke(cfg, monkeypatch, "on", "box").exit_code == 0
    assert H.load(cfg)[0]["enabled"] is True


def test_enrol_stdin_only_0600(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    blob = {"hub_name": "h", "host_name": "box", "token": "t", "secret": "s"}
    r = CliRunner().invoke(cli, ["hosts", "enrol"], input=json.dumps(blob) + "\n",
                           env={**os.environ, "NW_DATA_DIR": cfg["data_dir"],
                                "NW_CONFIG_DIR": cfg["config_dir"]})
    assert r.exit_code == 0
    p = F.federation_path(cfg)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert set(json.loads(p.read_text())) == set(blob)
    assert CliRunner().invoke(cli, ["hosts", "enrol", "x"], env={**os.environ,
                "NW_DATA_DIR": cfg["data_dir"], "NW_CONFIG_DIR": cfg["config_dir"]}).exit_code != 0
    assert CliRunner().invoke(cli, ["hosts", "enrol"], input="\n", env={**os.environ,
                "NW_DATA_DIR": cfg["data_dir"], "NW_CONFIG_DIR": cfg["config_dir"]}).exit_code != 0
    blob2 = {**blob, "token": "t2"}
    assert CliRunner().invoke(cli, ["hosts", "enrol"], input=json.dumps(blob2) + "\n",
                              env={**os.environ, "NW_DATA_DIR": cfg["data_dir"],
                                   "NW_CONFIG_DIR": cfg["config_dir"]}).exit_code == 0
    assert json.loads(p.read_text())["token"] == "t2"
    p.unlink()
    assert CliRunner().invoke(cli, ["hosts", "unenrol"], env={**os.environ,
                "NW_DATA_DIR": cfg["data_dir"], "NW_CONFIG_DIR": cfg["config_dir"]}).exit_code == 0
    help_out = CliRunner().invoke(cli, ["hosts", "--help"]).output
    for cmd in ("add", "list", "rm", "on", "off", "check", "enrol", "unenrol"):
        assert cmd in help_out


def test_serve_federate_refuses_without_federation_json(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["serve", "--federate"])
    assert r.exit_code != 0
    assert str(F.federation_path(cfg)) in r.output


def test_unenrol_running_server_returns_unsigned_401(tmp_path, monkeypatch):
    hub = _cfg(tmp_path, "hub")
    host = _cfg(tmp_path, "host")
    blob = _add_and_enrol(host, hub, monkeypatch)
    assert F.read_federation(host)
    srv, url = _serve(host)
    _point_hub(hub, "box", url)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    F.federation_path(host).unlink()
    st, hdrs, _ = _raw("GET", base, "/v1/status", blob["token"], blob["secret"])
    assert st == 401
    assert "X-NW-Sig" not in {k.title() for k in hdrs} and "x-nw-sig" not in hdrs
    st2, hdrs2, _ = _raw("GET", base, "/v1/status", blob["token"], blob["secret"])
    assert st2 == 401 and "x-nw-sig" not in hdrs2
    srv.shutdown()


def test_hmac_test_vector():
    assert F.sign("abc", "GET", "/v1/status", b"", 1700000000) == (
        "53f715f892cd9e350af654bcacaca46fd6dcedb104c690ffaa9060fac03a49b5")


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

    st, h, _ = req("GET", "/v1/status")
    assert st == 401 and (h.get("X-NW-Sig") or h.get("x-nw-sig"))
    hdrs = {**F.request_headers("abc", "GET", "/v1/status", b"", now=NOW), "Authorization": "Bearer tok"}
    st, h, raw = req("GET", "/v1/status", headers=hdrs)
    assert st == 200
    hdrs["Authorization"] = "Bearer wrong"
    assert req("GET", "/v1/status", headers=hdrs)[0] == 401
    hdrs = {**F.request_headers("abc", "GET", "/v1/status", b"", now=NOW + 121), "Authorization": "Bearer tok"}
    assert req("GET", "/v1/status", headers=hdrs)[0] == 401
    hdrs = {**F.request_headers("abc", "GET", "/v1/status", b"", now=NOW + 120), "Authorization": "Bearer tok"}
    assert req("GET", "/v1/status", headers=hdrs)[0] == 200
    post_hdrs = {**F.request_headers("abc", "POST", "/v1/status", b"", now=NOW + 120),
                 "Authorization": "Bearer tok"}
    st, h, _ = req("POST", "/v1/status", headers=post_hdrs)
    assert st == 405 and (h.get("X-NW-Sig") or h.get("x-nw-sig"))
    get_hdrs = {**F.request_headers("abc", "GET", "/nope", b"", now=NOW + 120),
                "Authorization": "Bearer tok"}
    st, h, _ = req("GET", "/nope", headers=get_hdrs)
    assert st == 404 and (h.get("X-NW-Sig") or h.get("x-nw-sig"))
    rev_hdrs = {**F.request_headers("abc", "POST", "/v1/revoke", b"", now=NOW + 120),
                "Authorization": "Bearer tok"}
    st, h, _ = req("POST", "/v1/revoke", headers=rev_hdrs)
    assert st == 404
    srv.shutdown()


def test_hubclient_rejects_bad_responses(tmp_path, monkeypatch):
    import time
    monkeypatch.setattr(time, "time", lambda: NOW)
    hub = _cfg(tmp_path, "hub")
    host = _cfg(tmp_path, "host")
    _add_and_enrol(host, hub, monkeypatch)
    srv, url = _serve(host)
    _point_hub(hub, "box", url)
    rec = H.load(hub)[0]
    client = F.HubClient(rec, hub, now=NOW)
    assert client.CONNECT_TIMEOUT == 2 and client.READ_TIMEOUT == 2
    assert client.get_json("/v1/status")["schema"] == "nw.federation.status.v1"
    rec2 = {**rec, "url": rec["url"] + "/"}
    F.HubClient(rec2, hub, now=NOW).get_json("/v1/status")
    with pytest.raises(ConnectionError):
        F.HubClient(rec, hub, now=NOW + 121).get_json("/v1/status")
    F.HubClient(rec, hub, now=NOW + 120).get_json("/v1/status")
    with pytest.raises(ConnectionError):
        F.HubClient(rec, hub, now=NOW).request("GET", "/v1/status-not-really")
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
    doc = FH.status_document(host)
    assert doc["schema"] == "nw.federation.status.v1"
    assert len(doc["campaigns"]) == 1
    assert doc["campaigns"][0]["campaign"] == "SPEC-1"
    assert doc["projects"] == ["P1"]
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1])
    hdrs = {**F.request_headers("abc", "GET", "/v1/lease", b"", now=NOW), "Authorization": "Bearer t"}
    conn.request("GET", "/v1/lease", headers=hdrs)
    assert conn.getresponse().status == 404
    srv.shutdown()
    assert not any("check_leases" in (t.name or "") for t in threading.enumerate())


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


def test_check_overlap_unreachable_and_disabled(tmp_path, monkeypatch):
    import time
    monkeypatch.setattr(time, "time", lambda: NOW)
    hub = _cfg(tmp_path, "hub")
    repo = make_repo(tmp_path)
    hub_full = make_cfg(tmp_path, repo, script(tmp_path, []))
    hub_full["config_dir"] = hub["config_dir"]
    hub_full["data_dir"] = hub["data_dir"]
    _campaign(hub_full, repo, "HUB-C", "QUEUED", project="Shared")
    assert _invoke(hub_full, monkeypatch, "list").exit_code == 0
    _invoke(hub_full, monkeypatch, "add", "a", "--url", "http://127.0.0.1:9")
    r_bad = _invoke(hub_full, monkeypatch, "check")
    assert r_bad.exit_code == 1 and "unreachable" in r_bad.output
    _invoke(hub_full, monkeypatch, "off", "a")
    assert "a off" in _invoke(hub_full, monkeypatch, "check").output
    assert _invoke(hub_full, monkeypatch, "check").exit_code == 0


def test_hub_modules_do_not_import_queue():
    root = Path(__file__).resolve().parents[1] / "src" / "nightwright" / "agentic"
    banned = {"hosts.py", "federation.py", "cli_hosts.py"}
    for name in banned:
        tree = ast.parse((root / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] != "queue"
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] != "queue"
    fh = ast.parse((root / "federation_host.py").read_text())
    calls = [n for n in ast.walk(fh) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "queue_payload"]
    assert len(calls) == 1
    assert "subprocess" not in (root / "federation_host.py").read_text()


def test_inert_without_registry_files(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    payload = Q.queue_payload(cfg)
    assert payload["schema"] == "nw.queue.v1"
    assert not H.path(cfg).exists() and not F.federation_path(cfg).exists()
    assert _invoke(cfg, monkeypatch, "list").exit_code == 0
    assert not H.path(cfg).exists()
