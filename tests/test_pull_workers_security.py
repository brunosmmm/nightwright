"""SPEC-0334: the pull-worker API's security rules. The API server runs in a thread on
127.0.0.1:0 (`test_pull_workers.Pull`); requests are raw `http.client` calls or the worker's
own `pull_worker.Client` / `Loop`, in process, unless a test needs `nw worker serve` itself."""
import hashlib
import http.client
import json
import shutil
import socket
import stat
import subprocess
import threading
import time

import pytest
from click.testing import CliRunner

from nightwright.agentic import pull_jobs as PJ
from nightwright.agentic import pull_worker as PL
from nightwright.agentic import remote_gates as RG
from nightwright.agentic import remote_jobs as RJ
from nightwright.agentic import workers as WK
from nightwright.cli import cli

from test_pull_workers import BW, JOB, Pull, _gate_bg, _leased
from test_remote_gates import CID, World
from test_remote_gates import _short_runtime_dir  # noqa: F401  (autouse fixture)
from waits import until


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


@pytest.fixture
def pull(world):
    p = Pull(world, names=("box", "box2"))
    yield p
    p.close()


def _call(p: Pull, method, path, token=None, body=None, length=None):
    conn = http.client.HTTPConnection("127.0.0.1", p.srv.server_address[1], timeout=30)
    try:
        conn.putrequest(method, path)
        if token is not None:
            conn.putheader("Authorization", f"Bearer {token}")
        data = b"" if body is None else (body if isinstance(body, bytes)
                                          else json.dumps(body).encode())
        conn.putheader("Content-Length", str(len(data) if length is None else length))
        conn.endheaders()
        if length is None and data:
            conn.send(data)
        r = conn.getresponse()
        raw = r.read()
        return r.status, (json.loads(raw) if raw.startswith(b"{") else raw)
    finally:
        conn.close()


def _queue(w: World, worker="box", job_id=JOB):
    """A real gate job queued for `worker` (as `remote_gates._run_async` queues it)."""
    rec = WK.load(w.cfg)
    wrec = next(r for r in rec if r["name"] == worker)
    bundle = w.tmp / f"{job_id}.bundle"
    header = RG.prepare(wrec, w.project, w.state, w.cfg, 1, job_id, bundle)
    header["attempt"] = 1
    now = time.time()
    RJ.create(w.cfg, job_id=job_id, campaign=CID, worker=worker, attempt=1, seq=1,
              submitted_at=now, deadline_at=now + 600, tier="loop")
    RJ.update(w.cfg, job_id, gates=[g["name"] for g in header["payload"]["gates"]])
    PJ.submit(w.cfg, wrec, header, bundle)
    return wrec


def _lease(w: World, worker="box"):
    wrec = next(r for r in WK.load(w.cfg) if r["name"] == worker)
    return PJ.lease(w.cfg, wrec, ["gate"], 1, 0, threading.Event())


def _loop(p: Pull, name="box", **doc):
    cfg = {"data_dir": str(p.wdata(name)), "config_dir": str(p.wcfg(name))}
    return PL.Loop(cfg, {**PJ.parse_blob(p.blobs[name]), **doc})


# ---- tokens, cross-worker access, rotation ---------------------------------------------------

def test_missing_wrong_and_revoked_tokens_get_401(world, pull, monkeypatch):
    lease = {"kinds": ["gate"], "slots_free": 1, "wait_s": 0}
    assert _call(pull, "POST", "/v1/lease", None, lease)[0] == 401
    assert _call(pull, "POST", "/v1/lease", "nope", lease)[0] == 401
    assert _call(pull, "POST", "/v1/lease", pull.tokens["box"], lease)[0] == 204
    monkeypatch.setattr("nightwright.cli.load_config", lambda: world.cfg)
    res = CliRunner().invoke(cli, ["workers", "rm", "box"])
    assert res.exit_code == 0, res.output
    assert _call(pull, "POST", "/v1/lease", pull.tokens["box"], lease)[0] == 401
    assert not PJ.secret_path(world.cfg, "box").exists()


def test_another_workers_job_is_403(world, pull):
    _queue(world)
    assert _lease(world) is not None
    frames = {"frames": []}
    assert _call(pull, "POST", f"/v1/jobs/{JOB}/frames", pull.tokens["box2"], frames)[0] == 403
    assert _call(pull, "GET", f"/v1/jobs/{JOB}/bundle", pull.tokens["box2"])[0] == 403
    assert _call(pull, "POST", f"/v1/jobs/{JOB}/frames", pull.tokens["box"], frames)[0] == 200


def test_rotation_invalidates_the_old_credential_at_once(world, pull, monkeypatch):
    lease = {"kinds": ["gate"], "slots_free": 1, "wait_s": 0}
    monkeypatch.setattr("nightwright.cli.load_config", lambda: world.cfg)
    old_secret = PJ.read_secret(world.cfg, "box")
    res = CliRunner().invoke(cli, ["workers", "token", "box", "--rotate"])
    assert res.exit_code == 0, res.output
    doc = PJ.parse_blob(res.output.strip().splitlines()[-1])
    assert _call(pull, "POST", "/v1/lease", pull.tokens["box"], lease)[0] == 401
    assert _call(pull, "POST", "/v1/lease", doc["token"], lease)[0] == 204
    assert doc["secret"] == PJ.read_secret(world.cfg, "box") != old_secret


def test_rotation_gives_up_the_workers_live_jobs(world, pull, monkeypatch):
    _queue(world)
    assert _lease(world) is not None
    monkeypatch.setattr("nightwright.cli.load_config", lambda: world.cfg)
    res = CliRunner().invoke(cli, ["workers", "token", "box", "--rotate"])
    assert res.exit_code == 0, res.output
    rec = RJ.read(world.cfg, JOB)
    assert rec["pull_state"] == "lost" and rec["pull_lost_reason"] == "revoked"


def test_a_heartbeat_for_another_workers_job_says_gone(world, pull):
    _queue(world)
    assert _lease(world) is not None
    st, body = _call(pull, "POST", "/v1/heartbeat", pull.tokens["box2"], {"jobs": [{"id": JOB}]})
    assert st == 200 and body["jobs"][JOB] == {"cancel": True, "gone": True}
    st, body = _call(pull, "POST", "/v1/heartbeat", pull.tokens["box"], {"jobs": [{"id": JOB}]})
    assert st == 200 and body["jobs"][JOB] == {"cancel": False, "gone": False}


# ---- leases racing the queue -----------------------------------------------------------------

def test_a_lease_racing_submit_is_never_reset_to_queued(world, pull, monkeypatch):
    """A worker already long-polling leases the job while `submit` is still marking the record
    `queued`: the record must end `leased`, and the job be handed out exactly once."""
    orig, leased = RJ.update, []

    def racing(cfg, job_id, **fields):
        if fields.get("pull_state") == "queued" and not leased:
            leased.append(_lease(world))        # the long-poll's `_take` gets in first
        return orig(cfg, job_id, **fields)
    monkeypatch.setattr(RJ, "update", racing)
    _queue(world)
    monkeypatch.setattr(RJ, "update", orig)
    got = [g for g in leased + [_lease(world)] if g is not None]
    assert len(got) == 1
    assert RJ.read(world.cfg, JOB)["pull_state"] == "leased"


def test_a_renewal_repairs_a_record_left_queued(world, pull):
    _queue(world)
    assert _lease(world) is not None
    RJ.update(world.cfg, JOB, pull_state="queued")
    st, _ = _call(pull, "POST", "/v1/heartbeat", pull.tokens["box"], {"jobs": [{"id": JOB}]})
    assert st == 200 and RJ.read(world.cfg, JOB)["pull_state"] == "leased"
    assert JOB in PJ.expire_leases(world.cfg, now=time.time() + 3600)


def test_a_lease_renewed_after_the_scan_is_not_expired(world, pull, monkeypatch):
    _queue(world)
    assert _lease(world) is not None
    orig = PJ.job_lock

    def renewed_first(cfg, job_id):           # a heartbeat held the lock across the scan
        RJ.update(cfg, job_id, lease_until=time.time() + 3600)
        return orig(cfg, job_id)
    RJ.update(world.cfg, JOB, lease_until=time.time() - 1)
    monkeypatch.setattr(PJ, "job_lock", renewed_first)
    assert PJ.expire_leases(world.cfg) == []
    assert RJ.read(world.cfg, JOB)["pull_state"] == "leased"


# ---- the worker's checks of a leased job -----------------------------------------------------

@pytest.mark.parametrize("case", ["tamper", "expired", "replay", "bundle"])
def test_a_job_failing_verification_is_refused_and_nothing_runs(world, pull, case):
    _queue(world)
    got = _lease(world)
    loop = _loop(pull)
    h, secret = got["job"], PJ.read_secret(world.cfg, "box")
    if case == "tamper":
        h["payload"]["gates"][0]["cmd"] = "touch /tmp/pwned"
    elif case == "expired":
        got["job"] = PJ.sign(secret, h, h["bundle_sha256"],
                             now=time.time() - PJ.EXPIRES_S - PJ.CLOCK_SKEW_S - 5)
    elif case == "replay":
        loop.remember(h["nonce"], h["expires_at"])
    else:
        p = PJ.queue_dir(world.cfg) / f"{JOB}.bundle"
        data = bytearray(p.read_bytes())
        data[len(data) // 2] ^= 0xFF
        p.write_bytes(bytes(data))
    loop.start(got)
    rec = RJ.read(world.cfg, JOB)
    assert rec["pull_state"] == "error" and rec["pull_error"]["stage"] == "verify"
    assert not loop.jobs
    assert not (pull.wdata() / "agentic" / "worker" / "jobs" / JOB).exists()
    (frame,) = PJ.read_spool(world.cfg, JOB)
    assert frame["type"] == "error" and frame["stage"] == "verify"


def test_a_verified_signature_covers_every_field():
    h = {"job_id": "j-1", "kind": "gate", "payload": {"gates": []}}
    s = PJ.sign("k" * 64, h, "0" * 64, now=1000)
    assert s["nonce"] == "j-1" and s["expires_at"] - s["issued_at"] == 15 * 60
    seen = set().__contains__
    assert PJ.verify("k" * 64, s, b"", seen, now=1001) == "bundle hash mismatch"
    s2 = PJ.sign("k" * 64, h, hashlib.sha256(b"x").hexdigest(), now=1000)
    assert PJ.verify("k" * 64, s2, b"x", seen, now=1001) is None
    assert PJ.verify("k" * 64, s2, b"x", seen, now=1000 + 15 * 60 + 119) is None   # skew
    assert PJ.verify("x" * 64, s2, b"x", seen, now=1001) == "bad signature"


# ---- the listener ----------------------------------------------------------------------------

@pytest.mark.parametrize("host,ok", [
    ("127.0.0.1", True), ("::1", True), ("localhost", True), ("100.64.0.1", True),
    ("100.127.255.254", True), ("fd7a:115c:a1e0::1", True), ("0.0.0.0", False),
    ("::", False), ("8.8.8.8", False), ("192.168.1.10", False), ("100.128.0.1", False),
    ("fd7a:115c:a1e1::1", False), ("example.com", False)])
def test_bind_safety_without_tls(host, ok):
    assert (PJ.check_bind(host, tls=False) is None) is ok
    assert PJ.check_bind(host, tls=True) is None


@pytest.mark.parametrize("bind", ["0.0.0.0:0", "[::]:0", "8.8.8.8:0"])
def test_a_refused_bind_never_starts(tmp_path, bind):
    cfg = {"data_dir": str(tmp_path / "d"), "config_dir": str(tmp_path / "c")}
    with pytest.raises(ValueError, match="refusing to bind"):
        PJ.make_server(cfg, bind)


def test_oversized_frames_and_bundles_get_413(world, pull):
    _queue(world)
    _lease(world)
    tok = pull.tokens["box"]
    st, _ = _call(pull, "POST", f"/v1/jobs/{JOB}/frames", tok, length=PJ.MAX_FRAMES_BYTES + 1)
    assert st == 413
    st, _ = _call(pull, "PUT", f"/v1/jobs/{JOB}/result-bundle", tok,
                  length=PJ.MAX_BUNDLE_BYTES + 1)
    assert st == 413
    st, _ = _call(pull, "POST", "/v1/heartbeat", tok, length=PJ.MAX_FRAMES_BYTES + 1)
    assert st == 413
    assert not [p for p in PJ.uploads_dir(world.cfg).iterdir() if p.name.startswith(".up-")]


def test_a_done_frame_before_its_result_bundle_gets_409(world, pull):
    job_id = f"{CID}-persona-001-implementer-a1"
    now = time.time()
    RJ.create(world.cfg, job_id=job_id, campaign=CID, worker="box", attempt=1, seq=0,
              submitted_at=now, deadline_at=now + 600, kind="persona")
    RJ.update(world.cfg, job_id, pull=True, pull_state="leased", pull_cursor=0)
    data = b"result bundle bytes"
    done = {"type": "done", "n": 1, "job_id": job_id, "ok": True,
            "bundle": {"head": "a" * 40, "wip": None, "bundle_b64": None},
            "result_bundle_sha256": hashlib.sha256(data).hexdigest()}
    tok = pull.tokens["box"]
    st, body = _call(pull, "POST", f"/v1/jobs/{job_id}/frames", tok, {"frames": [done]})
    assert st == 409 and body["cursor"] == 0
    assert RJ.read(world.cfg, job_id)["pull_state"] == "leased"
    assert _call(pull, "PUT", f"/v1/jobs/{job_id}/result-bundle", tok, data)[0] == 200
    st, body = _call(pull, "POST", f"/v1/jobs/{job_id}/frames", tok, {"frames": [done]})
    assert st == 200 and body["cursor"] == 1
    assert RJ.read(world.cfg, job_id)["pull_state"] == "done"
    assert PJ._with_bundle(world.cfg, job_id, done)["bundle"]["bundle_b64"]


def test_a_409_mid_batch_keeps_the_frames_before_it(world, pull):
    job_id = f"{CID}-persona-001-implementer-a1"
    now = time.time()
    RJ.create(world.cfg, job_id=job_id, campaign=CID, worker="box", attempt=1, seq=0,
              submitted_at=now, deadline_at=now + 600, kind="persona")
    RJ.update(world.cfg, job_id, pull=True, pull_state="leased", pull_cursor=0)
    data = b"result bundle bytes"
    frames = [{"type": "accepted", "n": 1, "job_id": job_id},
              {"type": "done", "n": 2, "job_id": job_id, "ok": True,
               "bundle": {"head": "a" * 40, "wip": None, "bundle_b64": None},
               "result_bundle_sha256": hashlib.sha256(data).hexdigest()}]
    tok = pull.tokens["box"]
    st, body = _call(pull, "POST", f"/v1/jobs/{job_id}/frames", tok, {"frames": frames})
    assert st == 409 and body["cursor"] == 1
    assert RJ.read(world.cfg, job_id)["pull_cursor"] == 1
    assert _call(pull, "PUT", f"/v1/jobs/{job_id}/result-bundle", tok, data)[0] == 200
    st, body = _call(pull, "POST", f"/v1/jobs/{job_id}/frames", tok, {"frames": frames})
    assert st == 200 and body["cursor"] == 2
    assert [f["n"] for f in PJ.read_spool(world.cfg, job_id)] == [1, 2]


# ---- secrets at rest -------------------------------------------------------------------------

def test_secret_and_coordinator_json_are_0600_and_the_registry_has_no_secret(tmp_path,
                                                                             monkeypatch):
    cfg = {"data_dir": str(tmp_path / "d"), "config_dir": str(tmp_path / "c"),
           "agentic": {"workers": {"coordinator_url": "http://100.64.0.7:8790"}}}
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    res = CliRunner().invoke(cli, ["workers", "add", "box", "--pull", "--kinds", "gate"])
    assert res.exit_code == 0, res.output
    blob = res.output.strip().splitlines()[-1]
    doc = PJ.parse_blob(blob)
    assert doc["coordinator_url"] == "http://100.64.0.7:8790" and doc["name"] == "box"
    key = PJ.secret_path(cfg, "box")
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    reg = WK.path(cfg).read_text()
    assert doc["secret"] not in reg and doc["token"] not in reg
    (rec,) = json.loads(reg)["workers"]
    assert rec["token_sha256"] == PJ.token_hash(doc["token"]) and rec["mode"] == "pull"
    wcfg = {"data_dir": str(tmp_path / "wd"), "config_dir": str(tmp_path / "wc")}
    assert PL.load_conf(wcfg, blob, None) == doc
    assert stat.S_IMODE(PL.conf_path(wcfg).stat().st_mode) == 0o600
    assert PL.load_conf(wcfg, None, None) == doc


# ---- revocation of a running worker ----------------------------------------------------------

def test_rm_revokes_a_running_worker_which_exits_and_its_job_runs_locally(tmp_path,
                                                                           monkeypatch):
    w = BW(tmp_path)
    p = Pull(w)
    try:
        proc = p.start()
        th, box = _gate_bg(w)
        assert until(lambda: _leased(w) and len(p.frames()) >= 3)
        monkeypatch.setattr("nightwright.cli.load_config", lambda: w.cfg)
        res = CliRunner().invoke(cli, ["workers", "rm", "box"])
        assert res.exit_code == 0 and "revoked" in res.output
        assert until(lambda: proc.poll() is not None)
        assert proc.returncode == PL.EXIT_REVOKED
        assert "401" in p.log()
        assert until(lambda: (p.job_dir() / "result.json").exists())
        w.release()
        th.join(60)
        run = box["run"]
        assert run.worker is None and run.ok    # given up, then run locally
        (lost,) = w.ledger(["worker_job_lost"])
        assert lost["job_id"] == JOB
    finally:
        w.release()
        p.close()


# ---- mTLS ------------------------------------------------------------------------------------

def _openssl(*args, cwd):
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def pki(tmp_path):
    if shutil.which("openssl") is None:
        pytest.skip("openssl is not installed")
    d = tmp_path / "pki"
    d.mkdir()
    _openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out",
             "ca.pem", "-days", "1", "-subj", "/CN=nw-test-ca",
             "-addext", "basicConstraints=critical,CA:TRUE",
             "-addext", "keyUsage=critical,keyCertSign,cRLSign", cwd=d)
    leaf = ("basicConstraints=CA:FALSE\nsubjectKeyIdentifier=hash\n"
            "authorityKeyIdentifier=keyid,issuer\n")
    (d / "leaf.ext").write_text(leaf)
    (d / "san.ext").write_text(leaf + "subjectAltName=IP:127.0.0.1\n")
    for cn, ext in (("127.0.0.1", ["-extfile", "san.ext"]), ("box", ["-extfile", "leaf.ext"]),
                    ("evil", ["-extfile", "leaf.ext"])):
        _openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{cn}.key", "-out",
                 f"{cn}.csr", "-subj", f"/CN={cn}", cwd=d)
        _openssl("x509", "-req", "-in", f"{cn}.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
                 "-CAcreateserial", "-out", f"{cn}.pem", "-days", "1", *ext, cwd=d)
    return d


def test_mtls_requires_the_workers_own_cn(world, pki):
    token, _secret = PJ.new_credential(world.cfg, "box")
    WK.path(world.cfg).parent.mkdir(parents=True, exist_ok=True)
    WK.path(world.cfg).write_text(json.dumps({"schema": WK.SCHEMA, "workers": [{
        "name": "box", "mode": "pull", "slots": 1, "token_sha256": PJ.token_hash(token)}]}))
    srv = PJ.make_server(world.cfg, "127.0.0.1:0", tls_cert=str(pki / "127.0.0.1.pem"),
                         tls_key=str(pki / "127.0.0.1.key"), client_ca=str(pki / "ca.pem"))
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                     daemon=True).start()
    url = f"https://127.0.0.1:{srv.server_address[1]}"
    lease = {"kinds": ["gate"], "slots_free": 1, "wait_s": 0}
    try:
        def client(cn):
            doc = {"coordinator_url": url, "token": token, "ca": str(pki / "ca.pem")}
            if cn:
                doc.update(cert=str(pki / f"{cn}.pem"), key=str(pki / f"{cn}.key"))
            return PL.Client(doc)
        assert client("box").json("POST", "/v1/lease", lease)[0] == 204
        assert client("evil").json("POST", "/v1/lease", lease)[0] == 403
        with pytest.raises(PL.Transport):
            client(None).json("POST", "/v1/lease", lease)
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_silent_tls_peer_never_blocks_other_requests(world, pki):
    token, _secret = PJ.new_credential(world.cfg, "box")
    WK.path(world.cfg).parent.mkdir(parents=True, exist_ok=True)
    WK.path(world.cfg).write_text(json.dumps({"schema": WK.SCHEMA, "workers": [{
        "name": "box", "mode": "pull", "slots": 1, "token_sha256": PJ.token_hash(token)}]}))
    srv = PJ.make_server(world.cfg, "127.0.0.1:0", tls_cert=str(pki / "127.0.0.1.pem"),
                         tls_key=str(pki / "127.0.0.1.key"))
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                     daemon=True).start()
    port = srv.server_address[1]
    idle = socket.create_connection(("127.0.0.1", port))   # never says a word
    try:
        doc = {"coordinator_url": f"https://127.0.0.1:{port}", "token": token,
               "ca": str(pki / "ca.pem")}
        lease = {"kinds": ["gate"], "slots_free": 1, "wait_s": 0}
        assert PL.Client(doc).json("POST", "/v1/lease", lease, timeout=10)[0] == 204
    finally:
        idle.close()
        srv.shutdown()
        srv.server_close()


def test_a_silent_connection_is_closed_after_the_handler_timeout(world, pull, monkeypatch):
    monkeypatch.setattr(PJ, "HANDLER_TIMEOUT_S", 0.5)
    monkeypatch.setattr(PJ._Handler, "timeout", 0.5)
    s = socket.create_connection(("127.0.0.1", pull.srv.server_address[1]))
    try:
        s.sendall(b"POST /v1/lease HTTP/1.1\r\n")      # half a request, then silence
        s.settimeout(30)
        assert s.recv(1024) == b""                         # the server hung up
    finally:
        s.close()


def test_tls_options_go_together(tmp_path):
    cfg = {"data_dir": str(tmp_path / "d"), "config_dir": str(tmp_path / "c")}
    with pytest.raises(ValueError):
        PJ.make_server(cfg, "127.0.0.1:0", tls_cert="x.pem")
    with pytest.raises(ValueError):
        PJ.make_server(cfg, "127.0.0.1:0", client_ca="ca.pem")


def test_a_disabled_worker_authenticates_but_leases_nothing(world, pull):
    _queue(world)

    def off(records):
        next(r for r in records if r["name"] == "box")["enabled"] = False
    WK.update(world.cfg, off)
    lease = {"kinds": ["gate"], "slots_free": 1, "wait_s": 0}
    assert _call(pull, "POST", "/v1/lease", pull.tokens["box"], lease)[0] == 204
    assert (PJ.queue_dir(world.cfg) / f"{JOB}.json").exists()      # still queued for it


def test_prune_drops_a_pull_jobs_spool_and_upload(world, pull):
    _queue(world)
    _lease(world)
    tok = pull.tokens["box"]
    assert _call(pull, "PUT", f"/v1/jobs/{JOB}/result-bundle", tok, b"x")[0] == 200
    assert _call(pull, "POST", f"/v1/jobs/{JOB}/frames", tok, {"frames": [
        {"type": "accepted", "n": 1, "job_id": JOB}]})[0] == 200
    assert PJ.spool_path(world.cfg, JOB).exists()
    RJ.update(world.cfg, JOB, state="local")
    assert RJ.prune(world.cfg, CID) == [JOB]
    assert not PJ.spool_path(world.cfg, JOB).exists()
    assert not PJ.result_bundle_path(world.cfg, JOB).exists()
