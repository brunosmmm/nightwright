"""SPEC-0334: pull workers against the coordinator job API. Offline: the API server
(`pull_jobs.make_server`) runs on 127.0.0.1:0 in a thread of the test process (the coordinator),
and `nw worker serve` runs as a subprocess against a second data dir (the worker). The fake
project and the SSH loopback to compare with are SPEC-0338/0341's (`test_remote_gates`)."""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from nightwright.agentic import gates as G
from nightwright.agentic import pull_jobs as PJ
from nightwright.agentic import remote_jobs as RJ
from nightwright.agentic import store as S
from nightwright.agentic import workers as WK

from test_remote_gates import CID, Remote, World, _gate, _snapshot
from test_remote_gates import _short_runtime_dir  # noqa: F401  (autouse fixture)
from waits import WAIT_S, until

ASYNC_INFO = {"protocol": "nw.job.v1", "features": ["async", "persona"], "git": "git version 2"}
JOB = RJ.gate_job_id(CID, 1)


class Pull:
    """The coordinator's API server plus pull workers' credentials and `nw worker serve`
    processes. `workers` maps a name to its enrolment blob."""

    def __init__(self, w: World, names=("box",), kinds=("gate", "persona"), check_every=0.2):
        self.w, self.tmp = w, w.tmp
        p = WK.path(w.cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.srv = PJ.make_server(w.cfg, "127.0.0.1:0", check_every=check_every)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.thread = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05},
                                       daemon=True)
        self.thread.start()
        self.blobs, self.tokens, self.procs = {}, {}, {}
        recs = []
        for name in names:
            token, secret = PJ.new_credential(w.cfg, name)
            self.tokens[name] = token
            self.blobs[name] = PJ.enrol_blob(self.url, name, token, secret)
            recs.append({"name": name, "mode": "pull", "slots": 1, "kinds": list(kinds),
                         "enabled": True, "cooldown_until": None,
                         "token_sha256": PJ.token_hash(token), "secret_ref": PJ.secret_ref(name)})
        p.write_text(json.dumps({"schema": WK.SCHEMA, "workers": recs}))

    def wdata(self, name="box") -> Path:
        return self.tmp / f"wdata-{name}" if name != "box" else self.tmp / "wdata"

    def wcfg(self, name="box") -> Path:
        return self.tmp / f"wcfgdir-{name}"

    def worker_config(self, name="box", **workers):
        d = self.wcfg(name)
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.yaml").write_text(json.dumps({"agentic": {"workers": workers}}))

    def start(self, name="box", env=None, *args):
        log = open(self.tmp / f"serve-{name}.log", "ab")
        e = {**os.environ, "NW_DATA_DIR": str(self.wdata(name)),
             "NW_CONFIG_DIR": str(self.wcfg(name)), "NW_WORKER_FINISH_GC": "0", **(env or {})}
        proc = subprocess.Popen([sys.executable, "-m", "nightwright", "worker", "serve",
                                 "--enrol", self.blobs[name], *args], env=e,
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        self.procs[name] = proc
        return proc

    def log(self, name="box") -> str:
        p = self.tmp / f"serve-{name}.log"
        return p.read_text(errors="replace") if p.exists() else ""

    def kill(self, name="box"):
        proc = self.procs.pop(name, None)
        if proc is not None and proc.poll() is None:
            proc.send_signal(signal.SIGCONT)
            proc.kill()
            proc.wait()

    def close(self):
        for name in list(self.procs):
            self.kill(name)
        self.srv.shutdown()
        self.srv.server_close()

    def job_dir(self, job_id=JOB, name="box") -> Path:
        return self.wdata(name) / "agentic" / "worker" / "jobs" / job_id

    def frames(self, job_id=JOB, name="box") -> list[dict]:
        p = self.job_dir(job_id, name) / "frames.jsonl"
        return [json.loads(ln) for ln in p.read_text().splitlines()] if p.is_file() else []


class BW(World):
    """SPEC-0341's blocking project: the third gate waits for a `GO` file."""

    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.go = tmp_path / "GO"
        self.project["gates"] = [
            _gate("g1", "echo one"), _gate("g2", "echo two"),
            _gate("g3", f"while [ ! -f {self.go} ]; do sleep 0.05; done; echo three")]

    def release(self):
        self.go.write_text("go\n")


@pytest.fixture
def bw(tmp_path):
    w = BW(tmp_path)
    yield w
    w.release()


@pytest.fixture
def pull(bw):
    p = Pull(bw)
    yield p
    p.close()


def _gate_bg(w, stop=None):
    """`run_and_log` in a thread; returns (thread, box) with box["run"] once it ends."""
    box = {}

    def target():
        if stop is None:
            box["run"] = G.run_and_log(w.cfg, w.state, w.project, by="test")
        else:                                   # no settle_job: only the stop may cancel
            box["run"] = G.run_all(w.project, w.state, w.cfg, seq=1, stop=stop)
    th = threading.Thread(target=target, daemon=True)
    th.start()
    return th, box


def _leased(w, job_id=JOB) -> bool:
    rec = RJ.read(w.cfg, job_id)
    return bool(rec) and rec.get("pull_state") == "leased" and int(rec.get("pull_cursor") or 0) >= 1


def _expiry_after_first_frame(monkeypatch, job_id=JOB):
    """Hold the server's lease check until `job_id`'s first frame is applied. A short
    `job_lease_s` (2 or 3 s) then cannot lapse while a loaded host is still starting the job
    process (bundle fetch, `WJ.submit`, the first frames post); from the first frame on the
    check runs as shipped, so a stopped worker's lease still expires."""
    real, opened = PJ.expire_leases, threading.Event()

    def gated(cfg, now=None):
        if not opened.is_set():
            if int((RJ.read(cfg, job_id) or {}).get("pull_cursor") or 0) < 1:
                return []
            opened.set()
        return real(cfg, now)
    monkeypatch.setattr(PJ, "expire_leases", gated)


def _seen_polling(monkeypatch, name):
    """An event set once worker `name` has sent its first `POST /v1/lease`: a test with a short
    `worker_ttl_s` queues its job only then, so a slow `nw worker serve` start-up is never taken
    for an unreachable worker."""
    real, seen = PJ.lease, threading.Event()

    def recording(cfg, worker, *a, **kw):
        if worker.get("name") == name:
            seen.set()
        return real(cfg, worker, *a, **kw)
    monkeypatch.setattr(PJ, "lease", recording)
    return seen


def _partition(monkeypatch, token):
    """An event that, while set, cuts the worker holding `token` off: the server answers each
    of its requests 503, so the worker backs off, keeps its jobs and renews no lease. (A
    SIGSTOP of `nw worker serve` is no partition: it can freeze the worker while it holds
    `jobs_lock`, which its job process then waits for before it may write `result.json`.)"""
    cut, real = threading.Event(), PJ._Handler._handle

    def handle(self, method):
        if cut.is_set() and self.headers.get("Authorization") == f"Bearer {token}":
            self.close_connection = True        # the body is unread
            return self._send(503, {"error": "partitioned (test)"})
        return real(self, method)
    monkeypatch.setattr(PJ._Handler, "_handle", handle)
    return cut


# ---- gate and persona jobs, compared with SSH push -------------------------------------------

def _ssh_run(tmp_path):
    w = World(tmp_path / "ssh")
    Remote(w).register(info=ASYNC_INFO)
    run = G.run_and_log(w.cfg, w.state, w.project, by="test")
    return w, run


def test_gate_job_matches_the_ssh_push_path(tmp_path):
    (tmp_path / "ssh").mkdir()
    (tmp_path / "pull").mkdir()
    sw, srun = _ssh_run(tmp_path)
    pw = World(tmp_path / "pull")
    p = Pull(pw)
    try:
        p.start()
        run = G.run_and_log(pw.cfg, pw.state, pw.project, by="test")
    finally:
        p.close()
    assert run.worker == "box" and srun.worker == "box" and run.job_id == srun.job_id == JOB
    a, b = _snapshot(pw, run), _snapshot(sw, srun)
    assert a["files"] == b["files"]             # the same gate files, byte for byte
    assert a["payload"] == b["payload"] and a["gate_run"] == b["gate_run"]
    assert [e["type"] for e in pw.ledger()] == [e["type"] for e in sw.ledger()]
    (pe,), (se,) = pw.ledger(["gate"]), sw.ledger(["gate"])
    assert pe["event_uid"] == se["event_uid"]
    rec = RJ.read(pw.cfg, JOB)
    assert rec["state"] == "done" and rec["pull_state"] == "done"
    assert rec["cursor"] == rec["pull_cursor"] == RJ.read(sw.cfg, JOB)["cursor"]


def test_persona_job_matches_the_ssh_push_path(tmp_path):
    from nightwright.agentic import remote_personas as RPS
    from nightwright.agentic import runner as R
    import test_remote_personas as TP
    script = TP._write_script(tmp_path / "writer.json", TP.WRITER)
    ssh_w, pull_w = TP._world(tmp_path, "ssh"), TP._world(tmp_path, "pull")
    TP._remote(ssh_w, script)
    want = RPS.dispatch(ssh_w.cfg, S.read_state(ssh_w.cfg, CID), "implement",
                        TP._req(ssh_w)).run(TP._req(ssh_w))
    p = Pull(pull_w)
    try:
        p.start(env={"NW_AGENTIC_RUNNER": "fake", "NW_AGENTIC_FAKE_SCRIPT": str(script),
                     "NW_WORKER_PROGRESS_S": "0.2"})
        rr = RPS.dispatch(pull_w.cfg, S.read_state(pull_w.cfg, CID), "implement",
                          TP._req(pull_w))
        assert isinstance(rr, R.RemoteRunner) and rr.worker["mode"] == "pull"
        got = rr.run(TP._req(pull_w))
    finally:
        p.close()
    assert got.worker == want.worker == "box"
    assert got.verdict == want.verdict == TP.VERDICT and got.verdict_ok
    a, b = TP._snap(pull_w.repo), TP._snap(ssh_w.repo)
    # the same write-back (tree, status, diff, new file); the worker's commit has its own date
    assert {k: v for k, v in a.items() if k != "head"} == \
        {k: v for k, v in b.items() if k != "head"}
    pd, sd = TP._req(pull_w).run_dir, TP._req(ssh_w).run_dir
    assert sorted(f.name for f in pd.iterdir()) == sorted(f.name for f in sd.iterdir())
    assert (pd / "result.json").read_bytes() == (sd / "result.json").read_bytes()
    assert not RJ.live(pull_w.cfg, CID)         # the record went, as over SSH
    job_id = RPS.job_id_for(CID, "001-implementer")
    assert not PJ.spool_path(pull_w.cfg, job_id).exists()


# ---- restart, lease expiry, long jobs, stop --------------------------------------------------

def test_restarted_worker_reposts_from_the_server_cursor(bw, pull):
    pull.start()
    th, box = _gate_bg(bw)
    assert until(lambda: _leased(bw) and len(pull.frames()) >= 3)
    pull.kill()                                 # mid-job: g3 still blocks
    pull.start()
    bw.release()
    th.join(60)
    run = box["run"]
    assert run.ok and run.worker == "box"
    spool = PJ.read_spool(bw.cfg, JOB)
    ns = [f["n"] for f in spool]
    assert ns == list(range(1, len(ns) + 1))    # every frame once, in order
    assert ns[-1] == pull.frames()[-1]["n"]
    assert len(bw.ledger(["gate"])) == 1


def test_lease_expiry_retries_then_runs_locally_and_the_old_worker_gets_410(tmp_path,
                                                                           monkeypatch):
    w = BW(tmp_path)
    w.cfg["agentic"]["workers"] = {"job_lease_s": 2, "worker_ttl_s": 3,
                                   "max_remote_attempts": 2}
    _expiry_after_first_frame(monkeypatch)
    polling = _seen_polling(monkeypatch, "box")
    p = Pull(w, names=("box", "box2"))
    cut = _partition(monkeypatch, p.tokens["box"])
    try:
        p.worker_config("box", job_lease_s=60)  # the coordinator's 2 s comes with the lease
        p.start("box")
        assert polling.wait(WAIT_S)
        th, box = _gate_bg(w)
        assert until(lambda: _leased(w))
        cut.set()                               # a partition: no heartbeat, no frames
        assert until(lambda: (RJ.read(w.cfg, JOB) or {}).get("state") == "lost")
        # its local lease (never renewed while cut off) ends the old job before g3 may finish
        assert until(lambda: (p.job_dir() / "result.json").exists())
        cut.clear()                             # healed: its next request is about a lost job
        w.release()                             # the retries' gates may finish now
        th.join(60)
        run = box["run"]
        assert run.worker is None and run.ok    # box2 never leased: local after 2 attempts
        retried = w.ledger(["worker_job_retried"])
        assert [(e["job_id"], e["to_worker"]) for e in retried] == [
            (RJ.gate_job_id(CID, 1, 2), "box2"), (RJ.gate_job_id(CID, 1, 3), None)]
        lost = w.ledger(["worker_job_lost"])
        assert [e["job_id"] for e in lost][:1] == [JOB]
        gone = f"{JOB}: the coordinator gave the job up (410)"
        assert until(lambda: gone in p.log("box"))
        # the worker cancels the job on the 410 (`nw worker cancel`), after the 410
        assert until(lambda: f"{JOB}: cancelling" in p.log("box").split(gone, 1)[1])
        assert until(lambda: (p.job_dir() / "result.json").exists())
        done = [f for f in p.frames() if f["type"] in ("done", "error")]
        assert done and (done[-1].get("interrupted") or done[-1]["type"] == "error")
    finally:
        p.close()


def test_a_job_longer_than_the_lease_completes_with_heartbeats(tmp_path, monkeypatch):
    w = World(tmp_path)
    _expiry_after_first_frame(monkeypatch)      # heartbeats are what keep the lease from then on
    # the gate outlives two whole leases; a 3 s lease (heartbeats every 1 s) leaves a loaded
    # test host 2 s of slack per heartbeat instead of 1.3 s
    w.project["gates"] = [_gate("slow", "sleep 7; echo slow")]
    w.cfg["agentic"]["workers"] = {"job_lease_s": 3}
    p = Pull(w)
    try:
        p.worker_config(job_lease_s=3)
        p.start()
        run = G.run_and_log(w.cfg, w.state, w.project, by="test")
    finally:
        p.close()
    assert run.ok and run.worker == "box" and not run.interrupted
    assert not w.ledger(["worker_job_lost"])


def test_a_stop_delivers_cancel_and_ends_the_job_within_5s(bw, pull):
    pull.start()
    stop = threading.Event()
    th, box = _gate_bg(bw, stop)
    assert until(lambda: _leased(bw) and len(pull.frames()) >= 3)
    t0 = time.monotonic()                       # the 5 s run from the stop itself
    stop.set()
    assert until(lambda: any(f["type"] == "done" for f in pull.frames()), timeout=5)
    assert time.monotonic() - t0 <= 5
    th.join(30)
    assert box["run"].interrupted
    rec = RJ.read(bw.cfg, JOB)
    assert rec["state"] == "cancelled" and rec["cancel_requested"]
    done = [f for f in pull.frames() if f["type"] == "done"][-1]
    assert done["interrupted"] is True
    assert "cancelling" in pull.log()
