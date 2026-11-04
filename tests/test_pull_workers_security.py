"""SPEC-0334: the pull-worker API's security rules. The API server runs in a thread on
127.0.0.1:0 (`test_pull_workers.Pull`); requests are raw `http.client` calls or the worker's
own `pull_worker.Client` / `Loop`, in process, unless a test needs `nw worker serve` itself."""
import base64
import gzip
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
import waits
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


def test_rotation_releases_the_workers_live_jobs_without_charging_it(world, pull, monkeypatch):
    """The old worker cancels its jobs on its next 401, so the steps release them with no
    cooldown and no recorded loss. The retry excludes the worker (it exits on that 401 until it
    is re-enrolled) and counts the attempt, as for any loss (SPEC-0342's budget)."""
    from nightwright.agentic import gates as G
    wrec = _queue(world)
    assert _lease(world) is not None
    monkeypatch.setattr("nightwright.cli.load_config", lambda: world.cfg)
    res = CliRunner().invoke(cli, ["workers", "token", "box", "--rotate"])
    assert res.exit_code == 0, res.output
    rec = RJ.read(world.cfg, JOB)
    assert rec["pull_state"] == "lost" and rec["pull_lost_reason"] == "rotated"
    with pytest.raises(RG._GiveUp) as gu:
        PJ.follow_gate(world.cfg, wrec, rec, world.project, 1, None)
    assert gu.value.stage == "rotated" and gu.value.cooldown is False
    charged = []
    monkeypatch.setattr(RG, "_record_loss", lambda *a: charged.append(("loss", a)))
    monkeypatch.setattr(RG, "set_cooldown", lambda *a: charged.append(("cooldown", a)))
    monkeypatch.setattr(RG, "_cancel_job", lambda *a: None)
    out = RG._give_up(world.cfg, CID, wrec, rec, 1, gu.value.stage)
    assert out.lost["state"] == "lost" and out.lost["lost_reason"] == "rotated"
    assert charged == []
    # the retry: attempt 2 goes to another worker, never back to the exited `box`
    lost = {**out.lost, "attempt": 1}
    seen = {}

    def pools(cfg, project_cfg, campaign_state, *, seq, excluded, worker=None):
        seen["excluded"] = list(excluded)
        return [("box2", 1), (WK.LOCAL, 1)]

    class Hold:
        pool, waited_s, interrupted = "box2", 0.0, False

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    run = G.GateRun(1, True, (), "now", 0.1, worker="box2")
    monkeypatch.setattr(RG, "retry_pools", pools)
    monkeypatch.setattr(G.gate_slots, "acquire", lambda *a, **k: Hold())
    monkeypatch.setattr(RG, "run", lambda *a, **k: (seen.update(attempt=k["attempt"]),
                                                    RG.Outcome(run))[1])
    got = G._retry(lost, 0.0, project_cfg=world.project, campaign_state=world.state,
                   cfg=world.cfg, cid=CID, worktree=world.tmp, seq=1, owner="gate-001",
                   stop=None, tier="loop", announce=None, worker=None, head=None, tree=None)
    assert got.ok and got.worker == "box2"
    assert seen == {"excluded": ["box"], "attempt": 2}
    assert charged == []
    # a rotated loss at attempt 2 of max_remote_attempts 2 runs locally: no extra attempt
    seen.clear()
    monkeypatch.setattr(G, "_fallback", lambda *a, **k: G.GateRun(1, True, (), "now", 0.1))
    got = G._retry({**out.lost, "attempt": 2}, 0.0, project_cfg=world.project,
                   campaign_state=world.state, cfg=world.cfg, cid=CID, worktree=world.tmp,
                   seq=1, owner="gate-001", stop=None, tier="loop", announce=None, worker=None,
                   head=None, tree=None)
    assert got.ok and got.worker is None and seen == {}


def test_a_rotation_whose_secret_is_not_written_is_undone(world, pull, monkeypatch):
    """`write_secret` fails after the registry took the new token: the registry gets the old
    token back, so the worker is not locked out, and its live job is left alone."""
    _queue(world)
    assert _lease(world) is not None
    lease = {"kinds": ["gate"], "slots_free": 1, "wait_s": 0}
    old_secret = PJ.read_secret(world.cfg, "box")
    monkeypatch.setattr("nightwright.cli.load_config", lambda: world.cfg)

    def full_disk(*a):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(PJ, "write_secret", full_disk)
    res = CliRunner().invoke(cli, ["workers", "token", "box", "--rotate"])
    assert res.exit_code != 0 and "undone" in res.output
    assert _call(pull, "POST", "/v1/lease", pull.tokens["box"], lease)[0] == 204
    assert PJ.read_secret(world.cfg, "box") == old_secret
    assert RJ.read(world.cfg, JOB)["pull_state"] == "leased"


def test_a_rotated_persona_job_goes_local_without_a_recorded_loss(world, monkeypatch):
    import types

    from nightwright.agentic import remote_personas as RPS
    job_id = f"{CID}-persona-001-implementer-a1"
    _persona_rec(world, job_id)
    PJ.give_up_lease(world.cfg, job_id, "rotated")
    calls = []
    monkeypatch.setattr(RG, "_cancel_job", lambda *a: None)
    monkeypatch.setattr(RPS.WK, "record_loss", lambda *a, **k: calls.append("loss"))
    monkeypatch.setattr(RPS, "_local", lambda rr, req, hooks: calls.append("local") or "ran")
    rr = types.SimpleNamespace(cfg=world.cfg, worker={"name": "box"}, state={"id": CID})
    req = types.SimpleNamespace(run_dir=str(world.tmp / "run"))
    assert PJ.follow_persona(rr, req, None, RJ.read(world.cfg, job_id)) == "ran"
    assert calls == ["local"]
    assert RJ.read(world.cfg, job_id)["lost_reason"] == "rotated"
    # SPEC-0345: the ledger names the run for the cap's clock, as an uncharged loss
    from nightwright.agentic import store as S
    ev, = S.read_ledger(world.cfg, CID, types=["worker_job_lost"])
    assert ev["charged"] is False and ev["run_id"] == "run"


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

@pytest.mark.parametrize("case", ["tamper", "expired", "replay", "replay-restart", "bundle"])
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
    elif case == "replay-restart":              # seen before the worker restarted
        _loop(pull).remember(h["nonce"], h["expires_at"])
        assert (pull.wdata() / "agentic" / "worker" / "nonces.jsonl").is_file()
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
        waits.join(th)
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
    assert _call(pull, "PUT", f"/v1/jobs/{JOB}/frame-upload", tok, b"{}")[0] == 200
    assert _call(pull, "POST", f"/v1/jobs/{JOB}/frames", tok, {"frames": [
        {"type": "accepted", "n": 1, "job_id": JOB}]})[0] == 200
    assert PJ.spool_path(world.cfg, JOB).exists() and PJ.frame_uploads(world.cfg, JOB)
    RJ.update(world.cfg, JOB, state="local")
    assert RJ.prune(world.cfg, CID) == [JOB]
    assert not PJ.spool_path(world.cfg, JOB).exists()
    assert not PJ.result_bundle_path(world.cfg, JOB).exists()
    assert not PJ.frame_uploads(world.cfg, JOB)


def test_a_body_of_the_wrong_shape_gets_400(world, pull):
    lease = {"kinds": ["gate"], "slots_free": 1, "wait_s": [1]}
    assert _call(pull, "POST", "/v1/lease", pull.tokens["box"], lease)[0] == 400


# ---- frames too big for a batch (out-of-band upload, isolation) ------------------------------

BIG = 5 << 20                           # one frame over the server's 4 MiB frames cap


def _persona_rec(w: World, job_id: str, worker="box"):
    now = time.time()
    RJ.create(w.cfg, job_id=job_id, campaign=CID, worker=worker, attempt=1, seq=0,
              submitted_at=now, deadline_at=now + 600, kind="persona")
    RJ.update(w.cfg, job_id, pull=True, pull_state="leased", pull_cursor=0,
              lease_until=now + 60)                 # live, and short of a renewal's 900 s


def _local_job(loop: PL.Loop, job_id: str, frames: list, done: bool):
    """A job dir on the worker holding `frames` (ended with `result.json` when `done`)."""
    job = loop._job(job_id)
    job.mkdir(parents=True)
    (job / "frames.jsonl").write_text("".join(json.dumps(f) + "\n" for f in frames))
    if done:
        (job / "result.json").write_text(json.dumps({"state": "done"}))
    loop.jobs[job_id] = {"posted": None, "cancelled": False}


def _big_done(job_id: str, n: int, bundle: bytes) -> dict:
    return {"schema": "nw-worker-frame/1", "type": "done", "job_id": job_id, "n": n, "ok": True,
            "files": {"transcript.jsonl": "x" * BIG},
            "bundle": {"head": "a" * 40, "wip": None,
                       "bundle_b64": base64.b64encode(bundle).decode()}}


def test_a_frame_stub_applies_only_once_its_upload_is_stored(world, pull):
    job_id = f"{CID}-persona-001-implementer-a1"
    _persona_rec(world, job_id)
    frame = {"type": "progress", "n": 1, "job_id": job_id, "ts": 1, "pad": "y" * 100}
    raw = json.dumps(frame).encode()
    sha = hashlib.sha256(raw).hexdigest()
    stub = {"type": "progress", "n": 1, "job_id": job_id, "frame_sha256": sha}
    tok = pull.tokens["box"]
    st, body = _call(pull, "POST", f"/v1/jobs/{job_id}/frames", tok, {"frames": [stub]})
    assert st == 409 and body["cursor"] == 0
    assert _call(pull, "PUT", f"/v1/jobs/{job_id}/frame-upload", tok, raw)[0] == 200
    assert _call(pull, "PUT", f"/v1/jobs/{job_id}/frame-upload", pull.tokens["box2"],
                 raw)[0] == 403
    st, body = _call(pull, "POST", f"/v1/jobs/{job_id}/frames", tok, {"frames": [stub]})
    assert st == 200 and body["cursor"] == 1 and body["final"] is False
    assert PJ.read_spool(world.cfg, job_id) == [frame]
    assert not PJ.frame_uploads(world.cfg, job_id)
    stub2 = {"type": "progress", "n": 2, "job_id": job_id, "frame_sha256": sha}
    assert _call(pull, "POST", f"/v1/jobs/{job_id}/frames", tok, {"frames": [stub2]})[0] == 409
    wrong = json.dumps({**frame, "n": 3}).encode()      # an upload that is not frame 2
    assert _call(pull, "PUT", f"/v1/jobs/{job_id}/frame-upload", tok, wrong)[0] == 200
    stub2["frame_sha256"] = hashlib.sha256(wrong).hexdigest()
    st, body = _call(pull, "POST", f"/v1/jobs/{job_id}/frames", tok, {"frames": [stub2]})
    assert st == 400 and body["cursor"] == 1


def test_a_frame_over_the_frames_cap_goes_up_out_of_band_and_the_job_ends(world, pull):
    big, small = f"{CID}-persona-001-implementer-a1", f"{CID}-persona-002-implementer-a1"
    _persona_rec(world, big)
    _persona_rec(world, small)
    loop = _loop(pull)
    loop.slots = 2
    bundle = b"result bundle bytes"
    _local_job(loop, big, [{"type": "accepted", "n": 1, "job_id": big},
                           _big_done(big, 2, bundle)], done=True)
    _local_job(loop, small, [{"type": "accepted", "n": 1, "job_id": small}], done=False)
    before = RJ.read(world.cfg, small)["lease_until"]
    loop.tick()
    rec = RJ.read(world.cfg, big)
    assert rec["pull_state"] == "done" and rec["pull_cursor"] == 2
    spooled = PJ.read_spool(world.cfg, big)[-1]
    assert spooled["files"]["transcript.jsonl"] == "x" * BIG and "frame_sha256" not in spooled
    assert PJ._with_bundle(world.cfg, big, spooled)["bundle"]["bundle_b64"] == \
        base64.b64encode(bundle).decode()
    assert not PJ.frame_uploads(world.cfg, big)
    assert big not in loop.jobs                         # ended and fully posted: dropped
    assert small in loop.jobs and RJ.read(world.cfg, small)["lease_until"] > before


def test_a_frame_over_the_upload_cap_becomes_an_error_frame(world, pull, monkeypatch):
    monkeypatch.setattr(PJ, "MAX_BUNDLE_BYTES", 1 << 20)
    job_id = f"{CID}-persona-001-implementer-a1"
    _persona_rec(world, job_id)
    loop = _loop(pull)
    frames = [{"type": "accepted", "n": 1, "job_id": job_id}, _big_done(job_id, 2, b"b"),
              {"type": "progress", "n": 3, "job_id": job_id, "ts": 2}]
    _local_job(loop, job_id, frames, done=False)
    cancelled = []
    monkeypatch.setattr(PL.WJ, "cancel", lambda cfg, jid, out: cancelled.append(jid))
    loop.sync(job_id)
    rec = RJ.read(world.cfg, job_id)
    assert rec["pull_state"] == "error" and rec["pull_cursor"] == 2
    assert rec["pull_error"]["stage"] == "frames" and "upload cap" in rec["pull_error"]["error"]
    assert cancelled == [job_id]                        # it ended there: stopped here
    (loop._job(job_id) / "result.json").write_text(json.dumps({"state": "done"}))
    loop.sync(job_id)
    assert job_id not in loop.jobs                      # the server takes no more: dropped


def test_one_jobs_failed_post_never_stops_the_others_or_the_heartbeat(world, pull):
    bad, good = f"{CID}-persona-001-implementer-a1", f"{CID}-persona-002-implementer-a1"
    _persona_rec(world, bad)
    _persona_rec(world, good)
    loop = _loop(pull)
    loop.slots = 3
    for jid in (bad, good):
        _local_job(loop, jid, [{"type": "accepted", "n": 1, "job_id": jid}], done=False)
    loop.next_hb = time.monotonic() + 3600              # not due: only the failure forces it
    real, calls = loop.client.call, []

    def call(method, path, *a, **kw):
        calls.append(path)
        if path == f"/v1/jobs/{bad}/frames":
            raise PL.Transport("connection reset")
        return real(method, path, *a, **kw)
    loop.client.call = call
    before = {j: RJ.read(world.cfg, j)["lease_until"] for j in (bad, good)}
    loop.tick()                                         # no exception escapes
    assert "/v1/heartbeat" in calls and "/v1/lease" in calls
    for jid in (bad, good):                             # both leases renewed
        assert RJ.read(world.cfg, jid)["lease_until"] > before[jid]
    assert RJ.read(world.cfg, good)["pull_cursor"] == 1
    assert bad in loop.jobs and loop.jobs[bad]["posted"] is None


def test_re_enrolling_keeps_the_tls_settings(tmp_path):
    cfg = {"config_dir": str(tmp_path), "data_dir": str(tmp_path)}
    blob = PJ.enrol_blob("https://c:8790", "box", "t1", "s1")
    PL.load_conf(cfg, blob, None)
    p = PL.conf_path(cfg)
    p.write_text(json.dumps({**json.loads(p.read_text()), "ca": "/ca.pem", "cert": "/c.pem",
                             "key": "/k.pem"}))
    doc = PL.load_conf(cfg, PJ.enrol_blob("https://c:8790", "box", "t2", "s2"), None)
    assert doc["token"] == "t2" and doc["ca"] == "/ca.pem" and doc["key"] == "/k.pem"
    assert json.loads(p.read_text())["cert"] == "/c.pem"


# ---- a cancelled job stays cancelled; a given-up job is gone ---------------------------------

def _gate_frame(w: World, n: int) -> dict:
    name = RJ.read(w.cfg, JOB)["gates"][0]
    return {"type": "gate", "n": n, "job_id": JOB,
            "result": {"name": name, "required": True, "ok": True, "exit_code": 0,
                       "timed_out": False, "duration_s": 0.1, "log": "", "tail": ""},
            "files": {f"001-{name}.log": base64.b64encode(gzip.compress(b"ok\n")).decode()}}


def _frames(p: Pull, job_id: str, *frames, worker="box"):
    return _call(p, "POST", f"/v1/jobs/{job_id}/frames", p.tokens[worker], {"frames": frames})


def _final_and_cancelled(w: World, job_id: str) -> dict:
    rec = RJ.read(w.cfg, job_id)
    assert rec["state"] == "cancelled"
    assert job_id not in [r["job_id"] for r in RJ.live(w.cfg)]
    assert "box" not in WK.draining_jobs(w.cfg)
    return rec


def test_a_cancelled_pull_gate_job_stays_cancelled_through_its_last_frames(world, pull):
    wrec = _queue(world)
    assert _lease(world) is not None
    assert _frames(pull, JOB, {"type": "started", "n": 1, "job_id": JOB})[0] == 200
    assert RJ.read(world.cfg, JOB)["state"] == "attached"
    assert RG.cancel_campaign(world.cfg, CID) == [JOB]          # a park or an abandon
    st, body = _frames(pull, JOB, _gate_frame(world, 2))        # a result lands after it
    assert st == 200 and body["cancel"] is True and body["cursor"] == 2
    _final_and_cancelled(world, JOB)
    st, body = _frames(pull, JOB, {"type": "done", "n": 3, "job_id": JOB, "ok": False,
                                   "interrupted": True, "duration_s": 0.2})
    assert st == 200 and body["final"] is True
    rec = _final_and_cancelled(world, JOB)
    assert rec["pull_state"] == "done" and "pull_error" not in rec  # a cancel, not an error
    out = PJ._replay_gate(world.cfg, wrec, rec, world.project, 1)
    assert out.run.interrupted and out.failure is None
    assert PJ.follow_gate(world.cfg, wrec, rec, world.project, 1, None).run.interrupted


def test_a_persona_step_waiting_through_a_park_never_runs_finish(world, pull, monkeypatch):
    import types

    from nightwright.agentic import remote_personas as RPS
    job_id = f"{CID}-persona-001-implementer-a1"
    _persona_rec(world, job_id)
    assert _frames(pull, job_id, {"type": "accepted", "n": 1, "job_id": job_id})[0] == 200
    assert RJ.read(world.cfg, job_id)["state"] == "attached"
    seen, polls = [], []
    monkeypatch.setattr(RPS, "_finish", lambda *a: seen.append("finish"))
    monkeypatch.setattr(RPS, "_lost", lambda rr, req, hooks, rec, why: seen.append(why))
    real_read = PJ.RJ.read
    monkeypatch.setattr(PJ.RJ, "read", lambda cfg, jid: (polls.append(jid), real_read(cfg, jid))[1])
    rr = types.SimpleNamespace(cfg=world.cfg)
    req = types.SimpleNamespace(run_dir=str(world.tmp / "run"))
    th = threading.Thread(target=PJ.follow_persona, args=(rr, req, None, RJ.read(world.cfg, job_id)),
                          daemon=True)
    th.start()
    until(lambda: polls)                                        # the step is waiting
    assert RG.cancel_campaign(world.cfg, CID) == [job_id]
    # within the step's 1 s tick, a progress frame and the interrupted `done`
    st, body = _frames(pull, job_id, {"type": "progress", "n": 2, "job_id": job_id, "ts": 1})
    assert st == 200 and body["cancel"] is True
    st, _ = _frames(pull, job_id, {"type": "done", "n": 3, "job_id": job_id, "ok": False,
                                   "interrupted": True,
                                   "bundle": {"head": "a" * 40, "wip": None, "bundle_b64": None}})
    assert st == 200
    rec = _final_and_cancelled(world, job_id)
    assert rec["pull_state"] == "done" and rec["pull_cursor"] == 3
    th.join(timeout=10)
    assert not th.is_alive() and seen == ["cancelled"]


def _gone_rec(w: World, job_id: str) -> None:
    RJ.update(w.cfg, job_id, pull_state="lost", pull_lost_reason="lease")


def test_every_request_for_a_given_up_job_gets_410(world, pull):
    _queue(world)
    assert _lease(world) is not None
    _gone_rec(world, JOB)
    tok = pull.tokens["box"]
    assert _frames(pull, JOB, {"type": "started", "n": 1, "job_id": JOB})[0] == 410
    assert _call(pull, "GET", f"/v1/jobs/{JOB}/bundle", tok)[0] == 410
    assert _call(pull, "PUT", f"/v1/jobs/{JOB}/result-bundle", tok, b"bytes")[0] == 410
    assert _call(pull, "PUT", f"/v1/jobs/{JOB}/frame-upload", tok, b"{}")[0] == 410
    st, body = _call(pull, "POST", "/v1/heartbeat", tok, {"jobs": [{"id": JOB}]})
    assert st == 200 and body["jobs"][JOB] == {"cancel": True, "gone": True}


@pytest.mark.parametrize("how", ["frames-410", "heartbeat-gone", "frames-403"])
def test_the_worker_cancels_and_drops_a_job_the_coordinator_refuses(world, pull, monkeypatch, how):
    job_id = f"{CID}-persona-001-implementer-a1"
    _persona_rec(world, job_id, worker="box2" if how == "frames-403" else "box")
    if how != "frames-403":
        _gone_rec(world, job_id)
    loop = _loop(pull)
    _local_job(loop, job_id, [{"type": "accepted", "n": 1, "job_id": job_id}], done=False)
    cancelled = []
    monkeypatch.setattr(PL.WJ, "cancel", lambda cfg, jid, out: cancelled.append(jid))
    if how == "heartbeat-gone":
        loop.heartbeat()
    else:
        loop.sync(job_id)
    assert cancelled == [job_id] and job_id not in loop.jobs
    assert job_id not in loop._read_tracked()


def test_a_final_job_takes_no_uploads_and_a_job_holds_few_frame_uploads(world, pull):
    job_id = f"{CID}-persona-001-implementer-a1"
    _persona_rec(world, job_id)
    tok = pull.tokens["box"]
    for i in range(PJ.MAX_FRAME_UPLOADS):
        assert _call(pull, "PUT", f"/v1/jobs/{job_id}/frame-upload", tok, b"%d" % i)[0] == 200
    assert _call(pull, "PUT", f"/v1/jobs/{job_id}/frame-upload", tok, b"0")[0] == 200  # a resend
    st, _ = _call(pull, "PUT", f"/v1/jobs/{job_id}/frame-upload", tok, b"one more")
    assert st == 409 and len(PJ.frame_uploads(world.cfg, job_id)) == PJ.MAX_FRAME_UPLOADS
    RJ.update(world.cfg, job_id, pull_state="error")
    for what in ("frame-upload", "result-bundle"):
        st, body = _call(pull, "PUT", f"/v1/jobs/{job_id}/{what}", tok, b"late")
        assert st == 409 and body["final"] is True
    assert not PJ.result_bundle_path(world.cfg, job_id).exists()
    assert not [p for p in PJ.uploads_dir(world.cfg).iterdir() if p.name.startswith(".up-")]


# ---- a torn spool line, lease input, re-uploads, the lease's lock ------------------------------

def _tear(w: World, job_id: str, frame: dict) -> None:
    """Half of `frame`'s spool line, unterminated: an append a crash or a full disk cut short."""
    line = json.dumps(frame)
    with open(PJ.spool_path(w.cfg, job_id), "a", encoding="utf-8") as f:
        f.write(line[: len(line) // 2])


def test_a_torn_spool_line_never_breaks_a_post_or_the_replay(world, pull):
    wrec = _queue(world)
    assert _lease(world) is not None
    names = RJ.read(world.cfg, JOB)["gates"]
    started = {"type": "started", "n": 1, "job_id": JOB}
    gates = []
    for i, name in enumerate(names):
        f = _gate_frame(world, 2 + i)
        f["result"] = {**f["result"], "name": name}
        f["files"] = {f"001-{name}.log": next(iter(f["files"].values()))}
        gates.append(f)
    done = {"type": "done", "n": 2 + len(names), "job_id": JOB, "ok": True,
            "duration_s": 0.2, "changed": []}
    assert _frames(pull, JOB, started)[0] == 200
    _tear(world, JOB, gates[0])
    assert PJ.read_spool(world.cfg, JOB) == [started]           # the fragment is skipped
    st, body = _frames(pull, JOB, *gates)                       # the worker re-posts them
    assert st == 200 and body["cursor"] == 1 + len(names)
    _tear(world, JOB, done)
    st, body = _frames(pull, JOB, done)
    assert st == 200 and body["cursor"] == done["n"] and body["final"] is True
    rec = RJ.read(world.cfg, JOB)
    assert rec["pull_state"] == "done"
    lines = PJ.spool_path(world.cfg, JOB).read_text().splitlines()
    assert [json.loads(ln)["n"] for ln in lines] == list(range(1, done["n"] + 1))
    out = PJ._replay_gate(world.cfg, wrec, rec, world.project, 1)
    assert out.run is not None and [r.name for r in out.run.results] == names
    # a spool whose `done` is the torn line: a protocol failure (the fallback), never a raise
    raw = PJ.spool_path(world.cfg, JOB).read_bytes()
    PJ.spool_path(world.cfg, JOB).write_bytes(raw[:-10])
    with pytest.raises(RG.RemoteFailure) as rf:
        PJ._replay_gate(world.cfg, wrec, rec, world.project, 1)
    assert rf.value.stage == "protocol"


def test_a_persona_done_whose_spool_line_is_torn_is_lost_not_raised(world, pull, monkeypatch):
    import types

    from nightwright.agentic import remote_personas as RPS
    job_id = f"{CID}-persona-001-implementer-a1"
    _persona_rec(world, job_id)
    assert _frames(pull, job_id, {"type": "accepted", "n": 1, "job_id": job_id})[0] == 200
    _tear(world, job_id, {"type": "done", "n": 2, "job_id": job_id, "ok": True})
    RJ.update(world.cfg, job_id, pull_state="done")     # as if the record said done
    seen = []
    monkeypatch.setattr(RPS, "_finish", lambda *a: seen.append("finish"))
    monkeypatch.setattr(RPS, "_lost", lambda rr, req, hooks, rec, why: seen.append(why))
    rr = types.SimpleNamespace(cfg=world.cfg)
    req = types.SimpleNamespace(run_dir=str(world.tmp / "run"))
    PJ.follow_persona(rr, req, None, RJ.read(world.cfg, job_id))
    assert seen == ["lost"]


def test_a_non_numeric_lease_wait_gets_400(world, pull):
    tok = pull.tokens["box"]
    for wait in ("soon", None, True, [1]):
        st, body = _call(pull, "POST", "/v1/lease", tok,
                         {"kinds": ["gate"], "slots_free": 1, "wait_s": wait})
        assert st == 400 and "wait_s" in body["error"]
    assert _call(pull, "POST", "/v1/lease", tok,
                 {"kinds": ["gate"], "slots_free": 1, "wait_s": 0.1})[0] == 204


def test_a_re_upload_of_an_applied_frame_never_counts_against_the_cap(world, pull):
    job_id = f"{CID}-persona-001-implementer-a1"
    _persona_rec(world, job_id)
    tok = pull.tokens["box"]
    raw = json.dumps({"type": "progress", "n": 1, "job_id": job_id, "ts": 1}).encode()
    sha = hashlib.sha256(raw).hexdigest()
    assert _call(pull, "PUT", f"/v1/jobs/{job_id}/frame-upload", tok, raw)[0] == 200
    stub = {"type": "progress", "n": 1, "job_id": job_id, "frame_sha256": sha}
    assert _frames(pull, job_id, stub)[1]["cursor"] == 1
    for i in range(PJ.MAX_FRAME_UPLOADS):                   # the job's cap is full
        assert _call(pull, "PUT", f"/v1/jobs/{job_id}/frame-upload", tok, b"%d" % i)[0] == 200
    st, body = _call(pull, "PUT", f"/v1/jobs/{job_id}/frame-upload", tok, raw)  # a retry
    assert st == 200 and body["sha256"] == sha
    assert not PJ.frame_upload_path(world.cfg, job_id, sha).exists()
    assert len(PJ.frame_uploads(world.cfg, job_id)) == PJ.MAX_FRAME_UPLOADS
    assert _frames(pull, job_id, stub)[1]["cursor"] == 1    # its stub is skipped


def test_a_lease_marks_the_record_leased_under_the_jobs_lock(world, pull, monkeypatch):
    import fcntl
    import os
    _queue(world)
    orig, held = PJ.RJ.update, []

    def check(cfg, job_id, **fields):
        if fields.get("pull_state") == "leased":
            fd = os.open(PJ.frames_dir(cfg) / f"{job_id}.lock", os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held.append(False)
            except BlockingIOError:
                held.append(True)
            finally:
                os.close(fd)
        return orig(cfg, job_id, **fields)
    monkeypatch.setattr(PJ.RJ, "update", check)
    assert _lease(world) is not None
    assert held == [True]


# ---- loop 7: the review's remaining gaps -----------------------------------------------------

def test_seen_nonces_survive_a_restart_until_they_expire(world, pull):
    now = time.time()
    first = _loop(pull)
    first.remember("old", now - PJ.CLOCK_SKEW_S - 10)       # expired, skew included
    first.remember("skewed", now - PJ.CLOCK_SKEW_S + 60)    # expired, inside the skew
    first.remember("live", now + PJ.EXPIRES_S)
    first.remember("newer", now + PJ.EXPIRES_S)             # this write prunes `old`
    again = _loop(pull)                                     # a restarted worker
    assert [again.seen(n) for n in ("old", "skewed", "live", "newer")] == \
        [False, True, True, True]
    lines = (pull.wdata() / "agentic" / "worker" / "nonces.jsonl").read_text().splitlines()
    assert [json.loads(ln)["nonce"] for ln in lines] == ["skewed", "live", "newer"]


def test_a_header_expired_inside_the_clock_skew_is_still_accepted():
    bundle = b"bundle"
    sha = hashlib.sha256(bundle).hexdigest()
    h = PJ.sign("s3cret", {"job_id": "j-1", "kind": "gate"}, sha, now=1_000_000.0)
    exp = h["expires_at"]
    assert exp == 1_000_000.0 + PJ.EXPIRES_S
    never = lambda n: False                                 # noqa: E731
    assert PJ.verify("s3cret", h, bundle, never, now=exp + 1) is None
    assert PJ.verify("s3cret", h, bundle, never, now=exp + PJ.CLOCK_SKEW_S - 1) is None
    assert PJ.verify("s3cret", h, bundle, never, now=exp + PJ.CLOCK_SKEW_S + 1) == \
        "the job has expired"


def test_a_body_without_content_length_gets_411(world, pull):
    conn = http.client.HTTPConnection("127.0.0.1", pull.srv.server_address[1], timeout=30)
    try:
        conn.putrequest("POST", "/v1/lease")
        conn.putheader("Authorization", f"Bearer {pull.tokens['box']}")
        conn.endheaders()
        r = conn.getresponse()
        assert r.status == 411 and b"Content-Length" in r.read()
    finally:
        conn.close()


def test_a_lease_long_poll_is_capped(world, pull, monkeypatch):
    monkeypatch.setattr(PJ, "LEASE_WAIT_S", 0.5)
    t0 = time.monotonic()
    st, _ = _call(pull, "POST", "/v1/lease", pull.tokens["box"],
                  {"kinds": ["gate"], "slots_free": 1, "wait_s": 3600})
    took = time.monotonic() - t0
    assert st == 204 and 0.5 <= took < 10


def test_a_heartbeat_id_that_is_not_a_job_id_builds_no_path(world, pull):
    bad = ["../../somewhere/x", "..", "a/b", ""]
    st, body = _call(pull, "POST", "/v1/heartbeat", pull.tokens["box"],
                     {"jobs": [{"id": i} for i in bad]})
    assert st == 200
    assert body["jobs"] == {i: {"cancel": False, "gone": False} for i in bad}
    root = world.tmp
    assert not list(root.rglob("x.lock")) and not list(root.rglob("x.json"))
    for i in bad:
        with pytest.raises(PJ.PullError) as e:
            PJ._job_for(world.cfg, "box", i)
        assert e.value.status == 400


def test_a_lease_racing_the_unreachable_give_up_wins(world, pull, monkeypatch):
    """The step read `queued` and its TTL ran out, but a lease took the job meanwhile: the
    step keeps waiting (here a stop ends it) instead of blaming the worker as `unreachable`.
    (The stop then drops the bundle, as any cancel of a leased job does.)"""
    wrec = _queue(world)
    assert PJ.unqueue_unleased(world.cfg, "no-such-job") is True
    real = PJ.RJ.read
    monkeypatch.setattr(PJ.RJ, "settings", lambda cfg: {
        "worker_ttl_s": -1.0, "job_lease_s": 900.0, "max_remote_attempts": 2})
    assert _lease(world) is not None                        # the lease wins the race
    rec, stale = RJ.read(world.cfg, JOB), []

    def read(cfg, jid):                                     # the step's own read is stale
        if jid == JOB and not stale:
            stale.append(1)
            return {**real(cfg, jid), "pull_state": "queued"}
        return real(cfg, jid)
    monkeypatch.setattr(PJ.RJ, "read", read)
    stop = threading.Event()
    stop.set()
    out = PJ.follow_gate(world.cfg, wrec, rec, world.project, 1, stop)
    assert stale == [1]
    assert out.run.interrupted
    after = real(world.cfg, JOB)
    assert after["pull_state"] == "leased" and after["state"] != "lost"
    assert "pull_lost_reason" not in after and "lost_reason" not in after
    assert PJ.unqueue_unleased(world.cfg, JOB) is False


def test_a_never_leased_job_is_unqueued_by_the_unreachable_give_up(world, pull, monkeypatch):
    wrec = _queue(world)
    monkeypatch.setattr(PJ.RJ, "settings", lambda cfg: {
        "worker_ttl_s": -1.0, "job_lease_s": 900.0, "max_remote_attempts": 2})
    with pytest.raises(RG._GiveUp) as gu:
        PJ.follow_gate(world.cfg, wrec, RJ.read(world.cfg, JOB), world.project, 1, None)
    assert gu.value.stage == "unreachable"
    assert not list(PJ.queue_dir(world.cfg).glob(f"{JOB}.*"))
    assert _lease(world) is None


def test_a_gate_jobs_frames_posts_never_replay_the_spool(world, pull, monkeypatch):
    """Each post feeds only its new frames to the `_Job` the previous post left: the spool is
    replayed once (the first post), not on every post."""
    _queue(world)
    assert _lease(world) is not None
    built = []
    orig = PJ._gate_job
    monkeypatch.setattr(PJ, "_gate_job", lambda *a: built.append(1) or orig(*a))
    names = RJ.read(world.cfg, JOB)["gates"]

    def gate(i):                                            # gate `names[i]` as frame i + 2
        f = _gate_frame(world, i + 2)
        f["result"] = {**f["result"], "name": names[i]}
        f["files"] = {f"001-{names[i]}.log": next(iter(f["files"].values()))}
        return f
    started = {"type": "started", "n": 1, "job_id": JOB, "slot_wait_s": 0}
    assert _frames(pull, JOB, started)[1]["cursor"] == 1
    for i in range(len(names) - 1):
        assert _frames(pull, JOB, gate(i))[1]["cursor"] == i + 2
        assert _frames(pull, JOB, gate(i))[1]["cursor"] == i + 2      # a re-post
    assert built == [1]
    PJ._forget_gate_job(world.cfg, JOB)                     # a server restart: rebuilt once
    last = len(names) + 1
    done = {"type": "done", "n": last + 1, "job_id": JOB, "ok": True, "interrupted": False,
            "duration_s": 0.2, "started_at": "now", "changed": []}
    st, body = _frames(pull, JOB, gate(len(names) - 1), done)
    assert built == [1, 1]
    assert st == 200 and body["final"] and body["cursor"] == last + 1
    assert [f["n"] for f in PJ.read_spool(world.cfg, JOB)] == list(range(1, last + 2))
    assert RJ.read(world.cfg, JOB)["pull_state"] == "done"
    assert PJ._gate_key(world.cfg, JOB) not in PJ._GATE_JOBS


def test_a_failed_bundle_fetch_ends_the_job_at_once(world, pull, monkeypatch):
    _queue(world)
    got = _lease(world)
    loop = _loop(pull)
    real = loop.client.call
    monkeypatch.setattr(loop.client, "call", lambda method, path, *a, **k: (
        (500, b"oops") if path.endswith("/bundle") else real(method, path, *a, **k)))
    loop.start(got)
    rec = RJ.read(world.cfg, JOB)
    assert rec["pull_state"] == "error" and rec["pull_error"]["stage"] == "bundle"
    assert not loop.jobs
    assert not (pull.wdata() / "agentic" / "worker" / "jobs" / JOB).exists()


def test_the_gate_job_cache_never_crosses_stores(tmp_path):
    """Two stores (two data dirs: two tests in one process) with the same job id at the same
    cursor never share a cached `_Job`."""
    a = {"data_dir": str(tmp_path / "a")}
    b = {"data_dir": str(tmp_path / "b")}
    job = type("J", (), {"seen": 2})()
    PJ._keep_gate_job(a, JOB, job)
    assert PJ._cached_gate_job(b, JOB, 2) is None
    assert PJ._cached_gate_job(a, JOB, 2) is job


# ---- an idle worker that may lease nothing, a long-poll whose client has gone ----------------

@pytest.mark.parametrize("why", ["disabled", "no kinds", "no free slot"])
def test_a_lease_that_can_get_nothing_still_waits(world, pull, why):
    """A disabled worker (or a request with no usable `kinds` / `slots_free`) waits its
    `wait_s` before the 204, so an idle one never spins against the coordinator."""
    lease = {"kinds": ["gate"], "slots_free": 1, "wait_s": 0.5}
    if why == "disabled":
        WK.update(world.cfg, lambda records: next(
            r for r in records if r["name"] == "box").update(enabled=False))
    elif why == "no kinds":
        lease["kinds"] = "gate"
    else:
        lease["slots_free"] = 0
    t0 = time.monotonic()
    assert _call(pull, "POST", "/v1/lease", pull.tokens["box"], lease)[0] == 204
    assert time.monotonic() - t0 >= 0.45


def test_a_204_that_did_not_wait_makes_the_worker_pause(world, pull, monkeypatch):
    """Against a coordinator that answers a long-poll at once, `tick` sleeps a tick."""
    monkeypatch.setattr(PL, "TICK_S", 0.4)
    loop = _loop(pull)
    calls = []
    monkeypatch.setattr(loop.client, "json", lambda *a, **k: calls.append(a) or (204, {}))
    t0 = time.monotonic()
    loop.tick()
    assert len(calls) == 1 and time.monotonic() - t0 >= 0.35


def _abandoned_long_poll(world, monkeypatch, connect, token):
    """A lease long-poll whose client hangs up while the server waits; a job is queued after
    the hang-up and before the server's next take. Returns what that take got."""
    entered, go, took = threading.Event(), threading.Event(), []
    real = PJ._take

    def take(cfg, worker, kinds, alive=lambda: True):
        if not entered.is_set():
            entered.set()
            go.wait(30)
        got = real(cfg, worker, kinds, alive)
        took.append(got)
        return got
    monkeypatch.setattr(PJ, "_take", take)
    s = connect()
    body = json.dumps({"kinds": ["gate"], "slots_free": 1, "wait_s": 30}).encode()
    s.sendall(b"POST /v1/lease HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer "
              + token.encode() + b"\r\nContent-Length: " + str(len(body)).encode()
              + b"\r\n\r\n" + body)
    assert until(entered.is_set)
    s.close()                                   # the worker restarted, its request is gone
    _queue(world)
    go.set()
    assert until(lambda: took)
    return took


def _still_queued(world):
    assert RJ.read(world.cfg, JOB)["pull_state"] == "queued"
    assert (PJ.queue_dir(world.cfg) / f"{JOB}.json").exists()
    assert _lease(world) is not None            # the next live lease gets it


def test_a_long_poll_whose_client_has_gone_takes_no_job(world, pull, monkeypatch):
    port = pull.srv.server_address[1]
    took = _abandoned_long_poll(world, monkeypatch,
                                lambda: socket.create_connection(("127.0.0.1", port)),
                                pull.tokens["box"])
    assert took == [None]
    _still_queued(world)


def test_a_tls_long_poll_whose_client_has_gone_takes_no_job(world, pull, pki, monkeypatch):
    import ssl
    srv = PJ.make_server(world.cfg, "127.0.0.1:0", tls_cert=str(pki / "127.0.0.1.pem"),
                         tls_key=str(pki / "127.0.0.1.key"))
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                     daemon=True).start()
    ctx = ssl.create_default_context(cafile=str(pki / "ca.pem"))

    def connect():
        raw = socket.create_connection(("127.0.0.1", srv.server_address[1]))
        return ctx.wrap_socket(raw, server_hostname="127.0.0.1")
    try:
        took = _abandoned_long_poll(world, monkeypatch, connect, pull.tokens["box"])
        assert took == [None]
        _still_queued(world)
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_live_long_poll_still_gets_a_job_queued_while_it_waits(world, pull):
    def later():
        until(lambda: False, timeout=0.6)
        _queue(world)
    t = threading.Thread(target=later)
    t.start()
    status, body = _call(pull, "POST", "/v1/lease", pull.tokens["box"],
                         {"kinds": ["gate"], "slots_free": 1, "wait_s": 10})
    t.join()
    assert status == 200 and body["job"]["job_id"] == JOB
    assert "entry" not in body and "leased_at" not in body
    assert RJ.read(world.cfg, JOB)["pull_state"] == "leased"


def _in_long_poll(monkeypatch):
    """An Event set once the server's lease long-poll has made its first take."""
    polling, real = threading.Event(), PJ._take

    def take(*a, **k):
        try:
            return real(*a, **k)
        finally:
            polling.set()
    monkeypatch.setattr(PJ, "_take", take)
    return polling


def _long_poll(pull, token, out):
    out.append(_call(pull, "POST", "/v1/lease", token,
                     {"kinds": ["gate"], "slots_free": 1, "wait_s": 10}))


def test_a_long_poll_on_a_rotated_token_takes_no_job(world, pull, monkeypatch):
    """The poll authenticated before the rotation: its takes check the token again, so the job
    queued after the rotation is not signed with the new secret and handed to the old worker."""
    polling, out = _in_long_poll(monkeypatch), []
    t = threading.Thread(target=_long_poll, args=(pull, pull.tokens["box"], out))
    t.start()
    assert polling.wait(10)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: world.cfg)
    res = CliRunner().invoke(cli, ["workers", "token", "box", "--rotate"])
    assert res.exit_code == 0, res.output
    new = PJ.parse_blob(res.output.strip().splitlines()[-1])["token"]
    _queue(world)
    t.join(20)
    assert not t.is_alive() and out[0][0] == 401
    rec = RJ.read(world.cfg, JOB)
    assert rec["pull_state"] == "queued" and "pull_lost_reason" not in rec
    assert (PJ.queue_dir(world.cfg) / f"{JOB}.json").exists()
    status, body = _call(pull, "POST", "/v1/lease", new,
                         {"kinds": ["gate"], "slots_free": 1, "wait_s": 0})
    assert status == 200 and body["job"]["job_id"] == JOB
    assert PJ.verify(PJ.read_secret(world.cfg, "box"), body["job"], (
        PJ.queue_dir(world.cfg) / f"{JOB}.bundle").read_bytes(), lambda n: False) is None


def test_a_long_poll_of_a_worker_disabled_meanwhile_takes_no_job(world, pull, monkeypatch):
    polling, out = _in_long_poll(monkeypatch), []
    t = threading.Thread(target=_long_poll, args=(pull, pull.tokens["box"], out))
    t.start()
    assert polling.wait(10)

    def off(records):
        next(r for r in records if r["name"] == "box")["enabled"] = False
    WK.update(world.cfg, off)
    _queue(world)
    t.join(20)
    assert not t.is_alive() and out[0][0] == 204
    assert RJ.read(world.cfg, JOB)["pull_state"] == "queued"
    assert (PJ.queue_dir(world.cfg) / f"{JOB}.json").exists()


def test_a_pending_refusal_keeps_the_lease_poll_short(world, pull, monkeypatch):
    """With no job running, a pending refusal is heartbeated every tick, so the lease poll is
    `TICK_S`, not `LEASE_WAIT_S`: a short `job_lease_s` cannot expire it between heartbeats."""
    loop = _loop(pull)
    loop.refusals["x"] = ("verify", "bad signature")
    beats, asked = [], []
    monkeypatch.setattr(loop, "report_refusals", lambda: None)
    monkeypatch.setattr(loop, "heartbeat", lambda: beats.append(1))
    monkeypatch.setattr(loop, "lease", lambda free, wait_s: asked.append(wait_s))
    loop.tick()
    assert beats == [1] and asked == [PL.TICK_S]
    loop.refusals.clear()
    loop.tick()
    assert asked == [PL.TICK_S, PJ.LEASE_WAIT_S]


def test_a_lease_answer_that_cannot_be_written_queues_the_job_again(world, pull, monkeypatch):
    _queue(world)
    real = PJ._Handler._send

    def send(self, status, body=None):
        if status == 200:
            raise BrokenPipeError("the client has gone")
        return real(self, status, body)
    monkeypatch.setattr(PJ._Handler, "_send", send)
    assert _call(pull, "POST", "/v1/lease", pull.tokens["box"],
                 {"kinds": ["gate"], "slots_free": 1, "wait_s": 0})[0] == 500
    monkeypatch.setattr(PJ._Handler, "_send", real)
    _still_queued(world)


def test_release_leaves_a_job_that_moved_on(world, pull):
    _queue(world)
    got = _lease(world)
    PJ.request_cancel(world.cfg, JOB)
    assert PJ.release_lease(world.cfg, got) is False
    assert RJ.read(world.cfg, JOB)["pull_state"] == "leased"
    assert not (PJ.queue_dir(world.cfg) / f"{JOB}.json").exists()


# ---- a leased job that cannot start is reported, not left to expire --------------------------

def test_a_bundle_fetch_transport_failure_is_reported_when_the_coordinator_is_back(
        world, pull, monkeypatch):
    _queue(world)
    got = _lease(world)
    loop = _loop(pull)
    real_call, real_json = loop.client.call, loop.client.json
    down = [True]

    def call(method, path, *a, **k):
        if down[0]:
            raise PL.Transport("ConnectionResetError: reset")
        return real_call(method, path, *a, **k)

    def post(method, path, *a, **k):
        if down[0]:
            raise PL.Transport("ConnectionRefusedError: refused")
        return real_json(method, path, *a, **k)
    monkeypatch.setattr(loop.client, "call", call)
    monkeypatch.setattr(loop.client, "json", post)
    loop.start(got)                             # no exception: the failure is the job's
    assert JOB in loop.refusals and not loop.jobs
    assert RJ.read(world.cfg, JOB)["pull_state"] == "leased"
    with pytest.raises(PL.Transport):           # still down: `run` backs off, refusal kept
        loop.tick()
    assert JOB in loop.refusals
    down[0] = False
    monkeypatch.setattr(PJ, "LEASE_WAIT_S", 0.2)
    loop.tick()                                 # the tick itself re-posts the refusal
    rec = RJ.read(world.cfg, JOB)
    assert rec["pull_state"] == "error" and rec["pull_error"]["stage"] == "bundle"
    assert not loop.refusals


def _refused_with_failing_posts(world, pull, monkeypatch):
    """A leased job whose bundle fetch failed, with every frames post answered 503."""
    _queue(world)
    got = _lease(world)
    loop = _loop(pull)
    real_call, real_json = loop.client.call, loop.client.json

    def call(method, path, *a, **k):
        if path.endswith("/bundle"):
            raise PL.Transport("ConnectionResetError: reset")
        return real_call(method, path, *a, **k)

    def post(method, path, *a, **k):
        if path.endswith("/frames"):
            return 503, {}
        return real_json(method, path, *a, **k)
    monkeypatch.setattr(loop.client, "call", call)
    monkeypatch.setattr(loop.client, "json", post)
    monkeypatch.setattr(PJ, "LEASE_WAIT_S", 0.2)
    loop.start(got)
    assert JOB in loop.refusals and not loop.jobs
    return loop


def test_a_pending_refusal_is_heartbeated_so_its_lease_does_not_expire(world, pull, monkeypatch):
    loop = _refused_with_failing_posts(world, pull, monkeypatch)
    RJ.update(world.cfg, JOB, lease_until=time.time() + 0.5)    # about to expire
    loop.tick()                                 # refusal post 503, heartbeat renews it
    rec = RJ.read(world.cfg, JOB)
    assert JOB in loop.refusals
    assert rec["pull_state"] == "leased"
    assert rec["lease_until"] > time.time() + RJ.settings(world.cfg)["job_lease_s"] - 5


def test_a_pending_refusal_whose_job_is_gone_is_dropped(world, pull, monkeypatch):
    loop = _refused_with_failing_posts(world, pull, monkeypatch)
    RJ.update(world.cfg, JOB, pull_state="lost", pull_lost_reason="lease")
    loop.tick()                                 # the heartbeat says gone: nothing to report
    assert not loop.refusals and not loop.jobs


def test_a_job_that_cannot_be_submitted_is_refused_with_stage_submit(world, pull, monkeypatch):
    _queue(world)
    got = _lease(world)
    loop = _loop(pull)

    def boom(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(PL.WJ, "submit", boom)
    loop.start(got)
    rec = RJ.read(world.cfg, JOB)
    assert rec["pull_state"] == "error" and rec["pull_error"]["stage"] == "submit"
    assert not loop.jobs and not loop.refusals


def test_a_nonce_that_cannot_be_stored_is_refused_with_stage_submit(world, pull, monkeypatch):
    _queue(world)
    got = _lease(world)
    loop = _loop(pull)
    submitted = []

    def boom(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(loop, "remember", boom)
    monkeypatch.setattr(PL.WJ, "submit", lambda *a, **k: submitted.append(a) or 0)
    loop.start(got)
    rec = RJ.read(world.cfg, JOB)
    assert rec["pull_state"] == "error" and rec["pull_error"]["stage"] == "submit"
    assert "No space left" in rec["pull_error"]["error"]
    assert submitted == []                      # nothing ran without its nonce stored
    assert not loop.jobs and not loop.refusals
    assert not (PL.W.jobs_dir(loop.cfg) / JOB).exists()


def test_an_add_whose_secret_cannot_be_written_leaves_no_worker(world, pull, monkeypatch):
    def boom(*a, **k):
        raise OSError(13, "Permission denied")
    monkeypatch.setattr("nightwright.cli.load_config", lambda: world.cfg)
    monkeypatch.setattr(PJ, "write_secret", boom)
    res = CliRunner().invoke(cli, ["workers", "add", "lap", "--pull"])
    assert res.exit_code != 0 and "could not write lap's secret" in res.output
    assert all(r["name"] != "lap" for r in WK.load(world.cfg))
    assert {r["name"] for r in WK.load(world.cfg)} == {"box", "box2"}
    assert not PJ.secret_path(world.cfg, "lap").exists()


def test_client_alive_works_for_a_connection_fd_past_fd_setsize(monkeypatch):
    """`_client_alive` must not call a connection with fd >= 1024 gone (`select.select` cannot
    take it; the long-poll would then never lease a job)."""
    import os
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    high = 1500
    if soft <= high:
        if hard != resource.RLIM_INFINITY and hard <= high:
            pytest.skip("RLIMIT_NOFILE too low for an fd past FD_SETSIZE")
        resource.setrlimit(resource.RLIMIT_NOFILE, (high + 16, hard))
    a, b = socket.socketpair()
    try:
        os.dup2(a.fileno(), high)
        conn = socket.socket(fileno=high)
        a.close()

        class H:
            connection = conn
        assert PJ._Handler._client_alive(H()) is True
        b.close()
        assert PJ._Handler._client_alive(H()) is False
        conn.close()
    finally:
        b.close()
        a.close()
        if soft <= high:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_an_add_that_loses_the_race_leaves_the_winners_secret(world, pull, monkeypatch):
    """Two `workers add box --pull` at once: the one whose registry update fails never writes
    or deletes the secret of the one that registered."""
    from nightwright.agentic import cli_workers as CW
    before = PJ.secret_path(world.cfg, "box").read_text()
    monkeypatch.setattr("nightwright.cli.load_config", lambda: world.cfg)
    monkeypatch.setattr(CW, "_load_or_fail", lambda cfg: [])   # past the early check
    res = CliRunner().invoke(cli, ["workers", "add", "box", "--pull"])
    assert res.exit_code != 0 and "already registered" in res.output
    assert PJ.secret_path(world.cfg, "box").read_text() == before
    assert _call(pull, "POST", "/v1/lease", pull.tokens["box"],
                 {"kinds": ["gate"], "slots_free": 1, "wait_s": 0})[0] == 204


# ---- the eighth review: update, the give-up's lock, a leased job's bundle --------------------

def test_workers_update_refuses_a_pull_worker_cleanly(world, pull, monkeypatch):
    from nightwright.agentic import workers_update as WU
    monkeypatch.setattr("nightwright.cli.load_config", lambda: world.cfg)
    monkeypatch.setattr(WU.WB, "build_wheel", lambda out: pytest.fail("no wheel for a pull worker"))
    for argv in (["workers", "update", "box"], ["workers", "update", "box", "--when-idle"]):
        res = CliRunner().invoke(cli, argv)
        assert res.exit_code == 1 and not isinstance(res.exception, KeyError), res.output
        assert "pull worker" in res.output
    rec = next(r for r in WK.load(world.cfg) if r["name"] == "box")
    assert not rec.get("draining") and not WK.update_pending(rec)    # never drained for it
    out = WU.perform(world.cfg, WK.normalize(world.cfg, rec))        # the queue's update pass
    assert out.kind == "failed" and "pull worker" in out.detail


def test_the_give_up_marks_a_pull_gate_job_lost_under_its_lock(world, pull, monkeypatch):
    """A frames post holds the job's lock while it writes the gate files: the give-up's `lost`
    waits for it, so a post after the give-up gets 410 and the files deleted stay deleted."""
    import fcntl
    import os

    from nightwright.agentic import store
    wrec = _queue(world)
    assert _lease(world) is not None
    assert _frames(pull, JOB, _gate_frame(world, 1))[0] == 200
    gdir = store.gates_dir(world.cfg, CID)
    assert list(gdir.glob("001-*"))
    orig, held = RG.RJ.update, []

    def check(cfg, job_id, **fields):
        if fields.get("state") == "lost":
            fd = os.open(PJ.frames_dir(cfg) / f"{job_id}.lock", os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held.append(False)
            except BlockingIOError:
                held.append(True)
            finally:
                os.close(fd)
        return orig(cfg, job_id, **fields)
    monkeypatch.setattr(RG.RJ, "update", check)
    RG._give_up(world.cfg, CID, wrec, RJ.read(world.cfg, JOB), 1, "deadline")
    assert held == [True]
    assert not list(gdir.glob("001-*"))
    assert _frames(pull, JOB, _gate_frame(world, 2))[0] == 410
    assert not list(gdir.glob("001-*"))


@pytest.mark.parametrize("how", ["give-up", "stop"])
def test_a_leased_job_given_up_or_stopped_drops_its_bundle(world, pull, how):
    wrec = _queue(world)
    assert _lease(world) is not None
    bundle = PJ.queue_dir(world.cfg) / f"{JOB}.bundle"
    assert bundle.is_file()
    if how == "give-up":
        RG._give_up(world.cfg, CID, wrec, RJ.read(world.cfg, JOB), 1, "deadline")
    else:
        RJ.update(world.cfg, JOB, state="cancelled")            # as `follow_gate`'s stop does
        PJ.request_cancel(world.cfg, JOB)
    assert not bundle.exists() and not (PJ.queue_dir(world.cfg) / f"{JOB}.json").exists()
    assert _call(pull, "GET", f"/v1/jobs/{JOB}/bundle", pull.tokens["box"])[0] == 410
