"""SPEC-0487: CLI persona runs (`nw spec review`) execute in the runner, which holds the
credentials. The runner side is driven in process (`RJ.service` with an injected spawn); the
real runner, `pass` and any real backend are never touched."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

from test_agentic_e2e import make_cfg, make_repo, run_cli, script
from waits import join, until
from nightwright import doctor
from nightwright.agentic import lease as LS
from nightwright.agentic import queue as Q
from nightwright.agentic import runner_jobs as RJ
from nightwright.agentic import runproc as RP
from nightwright.agentic import spec_review as SR

pytestmark = pytest.mark.fast

SECRET = "sk-cursor-SECRET-runner-jobs-42"
APPROVE = {"verdict": "approve", "size": "m", "tone": "high-end", "rationale": "small.",
           "findings": [], "suggested_splits": []}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(RJ.JOB_ENV, raising=False)
    monkeypatch.delenv("NW_AGENTIC_RUNNER", raising=False)
    monkeypatch.setenv("CURSOR_API_KEY", "placeholder")     # restored after the test
    monkeypatch.delenv("CURSOR_API_KEY")


def _setup(tmp_path):
    repo = make_repo(tmp_path, review=False)
    entries = [{"persona": "spec_reviewer", "verdict": APPROVE, "cost_usd": 0.1}]
    return repo, make_cfg(tmp_path, repo, script(tmp_path, entries))


def _spec(repo):
    return repo / "docs" / "specs" / "0001-trivial.md"


def _fields(repo):
    return {"spec_id": "SPEC-0001", "spec_path": str(_spec(repo).resolve()), "origin": None,
            "profile": None}


class InProc:
    """A spawn that records argv/env and runs the child in this process."""

    def __init__(self, cfg):
        self.cfg, self.envs = cfg, []

    def __call__(self, job, env):
        self.envs.append(env)
        RJ.run_job(self.cfg, job)
        return types.SimpleNamespace(pid=os.getpid())


def _serving_lease(cfg, **info):
    lease = LS.acquire(cfg, ["nw", "queue", "run"])
    lease.update_info(credentials={}, **info)
    return lease


def test_submit_service_wait_round_trip(tmp_path):
    repo, cfg = _setup(tmp_path)
    spawn = InProc(cfg)
    with _serving_lease(cfg):
        job = RJ.submit(cfg, "spec_review", _fields(repo))
        assert (job / "request.json").is_file()
        assert RJ.service(cfg, spawn=spawn, environ={"PATH": "/x"}) == [job.name]
        res = RJ.wait(cfg, job, poll_s=0.01)
    assert res["ok"] is True and res["review"]["verdict"] == "approve"
    assert not (job / "request.json").exists() and (job / "claimed.json").is_file()
    assert json.loads((job / "child.json").read_text())["pid"] == os.getpid()
    assert spawn.envs[0][RJ.JOB_ENV] == job.name and spawn.envs[0]["PATH"] == "/x"
    assert SR.load_review(cfg, "SPEC-0001", SR.spec_sha256(_spec(repo)))["verdict"] == "approve"
    started = [r for r in Q.read_journal(cfg) if r["kind"] == "runner_job_started"]
    assert started and started[0]["job"] == job.name and started[0]["spec_id"] == "SPEC-0001"
    assert RJ.service(cfg, spawn=spawn) == []                    # nothing left to claim


def test_real_spawn_argv(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(RJ.subprocess, "Popen",
                        lambda argv, **kw: calls.append((argv, kw)) or types.SimpleNamespace(pid=7))
    job = tmp_path / "j"
    job.mkdir()
    RJ._spawn(job, {"A": "1"})
    argv, kw = calls[0]
    assert argv == [sys.executable, "-m", "nightwright", "queue", "runner-job", str(job)]
    assert kw["env"] == {"A": "1"} and kw["start_new_session"] is True


def test_cli_round_trip_via_the_runner(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path)
    stop = threading.Event()
    spawn = InProc(cfg)

    def serve():
        while not stop.is_set():
            RJ.service(cfg, spawn=spawn)
            stop.wait(0.02)

    with _serving_lease(cfg):
        t = threading.Thread(target=serve, daemon=True)
        t.start()
        try:
            r = run_cli(cfg, monkeypatch, ["spec", "review", "SPEC-0001", "--json"])
        finally:
            stop.set()
            join(t)
    assert r.exit_code == 0, r.output
    assert "via the runner" in r.stderr
    out = json.loads(r.stdout)
    side = SR.load_review(cfg, "SPEC-0001", SR.spec_sha256(_spec(repo)))
    out.pop("profile", None)
    assert out == side and len(spawn.envs) == 1


def test_no_secret_in_job_files(tmp_path):
    repo, cfg = _setup(tmp_path)
    with _serving_lease(cfg):
        job = RJ.submit(cfg, "spec_review", _fields(repo))
        RJ.service(cfg, spawn=InProc(cfg), environ={"CURSOR_API_KEY": SECRET})
        RJ.wait(cfg, job, poll_s=0.01)
    files = [f for f in RJ.jobs_dir(cfg).rglob("*") if f.is_file()]
    assert {f.name for f in files} >= {"claimed.json", "child.json", "result.json"}
    for f in files:
        assert SECRET not in f.read_text(errors="replace"), f


def test_claim_timeout_cancels(tmp_path):
    repo, cfg = _setup(tmp_path)
    with _serving_lease(cfg):
        job = RJ.submit(cfg, "spec_review", _fields(repo))
        with pytest.raises(ValueError, match="nw doctor") as e:
            RJ.wait(cfg, job, claim_timeout_s=0, poll_s=0.01)
    assert "not claimed" in str(e.value)
    assert (job / "cancelled.json").is_file() and not (job / "request.json").exists()


def test_runner_gone_while_waiting_cancels(tmp_path):
    repo, cfg = _setup(tmp_path)
    lease = _serving_lease(cfg)
    job = RJ.submit(cfg, "spec_review", _fields(repo))
    polls = []

    def sleep(_s):
        polls.append(1)
        lease.release()

    with pytest.raises(ValueError, match="the runner stopped"):
        RJ.wait(cfg, job, sleep=sleep)
    assert polls and (job / "cancelled.json").is_file()
    assert RJ.service(cfg, spawn=InProc(cfg)) == []             # a cancelled job never runs


def test_dead_child_without_result(tmp_path):
    repo, cfg = _setup(tmp_path)
    job = RJ.submit(cfg, "spec_review", _fields(repo))
    os.rename(job / "request.json", job / "claimed.json")
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()                                                     # exited and reaped: gone
    (job / "child.json").write_text(json.dumps({"pid": p.pid}))
    t = [0.0]

    def clock():
        return t[0]

    def sleep(s):
        t[0] += 1.0

    with pytest.raises(ValueError, match="job.log"):
        RJ.wait(cfg, job, clock=clock, sleep=sleep)


def test_claim_and_cancel_race(tmp_path):
    repo, cfg = _setup(tmp_path)
    a = RJ.submit(cfg, "spec_review", _fields(repo))
    assert RJ.service(cfg, spawn=lambda job, env: types.SimpleNamespace(pid=None)) == [a.name]
    assert RJ.cancel(a) is False                                 # claimed first
    b = RJ.submit(cfg, "spec_review", _fields(repo))
    assert RJ.cancel(b) is True
    assert RJ.service(cfg, spawn=InProc(cfg)) == []              # cancelled first


def test_service_honours_stop_and_spawn_errors(tmp_path):
    repo, cfg = _setup(tmp_path)
    job = RJ.submit(cfg, "spec_review", _fields(repo))
    stop = threading.Event()
    stop.set()
    assert RJ.service(cfg, spawn=InProc(cfg), stop=stop) == []
    assert (job / "request.json").is_file()

    def boom(job, env):
        raise OSError("no exec")

    assert RJ.service(cfg, spawn=boom) == [job.name]
    res = json.loads((job / "result.json").read_text())
    assert res["ok"] is False and "no exec" in res["error"]


def test_delegate_conditions(tmp_path, monkeypatch):
    _repo, cfg = _setup(tmp_path)
    assert RJ.delegate(cfg) is None                              # no lease
    lease = LS.acquire(cfg, ["nw", "queue", "run"])
    try:
        assert RJ.runner_here(cfg) is not None
        assert RJ.delegate(cfg) is None                          # an old runner: no credentials
        lease.update_info(credentials={})
        assert RJ.delegate(cfg)["pid"] == os.getpid()
        monkeypatch.setenv(RJ.JOB_ENV, "x")
        assert RJ.delegate(cfg) is None                          # already a runner job
        monkeypatch.delenv(RJ.JOB_ENV)
        lease.update_info(host="elsewhere.example")
        assert RJ.delegate(cfg) is None                          # another host's runner
    finally:
        lease.release()


def _cursor(cfg):
    cfg["agentic"]["runner"] = "cursor"
    cfg["agentic"]["credentials"] = {"CURSOR_API_KEY": "pass:cursor/sdk"}
    return cfg


def test_no_key_errors_before_any_journal(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path)
    _cursor(cfg)
    with pytest.raises(ValueError, match="no runner is up") as e:
        SR.run_spec_review(cfg, _spec(repo), "SPEC-0001")
    assert "nw queue install" in str(e.value) and "nw doctor" in str(e.value)
    with LS.acquire(cfg, ["nw", "queue", "run"]):                # an old runner
        with pytest.raises(ValueError, match="predates runner jobs") as e:
            SR.run_spec_review(cfg, _spec(repo), "SPEC-0001")
        assert "nw queue restart" in str(e.value)
    monkeypatch.setenv(RJ.JOB_ENV, "job-1")
    with pytest.raises(ValueError, match="the runner has no CURSOR_API_KEY") as e:
        SR.run_spec_review(cfg, _spec(repo), "SPEC-0001")
    assert "pass:cursor/sdk" in str(e.value)
    assert [r for r in Q.read_journal(cfg) if "spec_review" in str(r.get("kind"))] == []
    assert SR.load_review(cfg, "SPEC-0001", SR.spec_sha256(_spec(repo))) is None


def test_run_job_bad_path_and_bad_doc(tmp_path):
    repo, cfg = _setup(tmp_path)
    job = RJ.submit(cfg, "spec_review", {**_fields(repo), "spec_path": str(tmp_path / "nope.md")})
    os.rename(job / "request.json", job / "claimed.json")
    assert RJ.run_job(cfg, job) != 0
    res = json.loads((job / "result.json").read_text())
    assert res["ok"] is False and res["error"] and res["finished_at"]
    other = tmp_path / "other"
    other.mkdir()
    assert RJ.run_job(cfg, other) != 0
    assert "not a claimed" in json.loads((other / "result.json").read_text())["error"]


def test_runner_job_command_is_hidden_but_runs(tmp_path, monkeypatch):
    _repo, cfg = _setup(tmp_path)
    r = run_cli(cfg, monkeypatch, ["queue", "--help"])
    assert "runner-job" not in r.output
    r = run_cli(cfg, monkeypatch, ["queue", "runner-job", "--help"])
    assert r.exit_code == 0 and "JOB_DIR" in r.output


# ---- doctor ----------------------------------------------------------------------------------

def test_doctor_credentials_check(tmp_path):
    _repo, cfg = _setup(tmp_path)
    assert doctor.check_credentials(None)["status"] == "skip"
    assert doctor.check_credentials(cfg)["status"] == "skip"     # nothing configured
    _cursor(cfg)
    c = doctor.check_credentials(cfg)
    assert c["status"] == "warn" and "nw queue install" in c["fix"]
    lease = LS.acquire(cfg, ["nw", "queue", "run"])
    try:
        c = doctor.check_credentials(cfg)
        assert c["status"] == "warn" and "restart" in c["fix"]   # an old runner
        lease.update_info(credentials={"CURSOR_API_KEY": "resolved"})
        c = doctor.check_credentials(cfg)
        assert c["status"] == "ok" and "CURSOR_API_KEY" in c["summary"]
        lease.update_info(credentials={"CURSOR_API_KEY": "locked"})
        c = doctor.check_credentials(cfg)
        assert c["status"] == "fail" and "locked" in c["summary"]
        assert "pass:cursor/sdk" in c["summary"] and "unlock gpg" in c["fix"]
        lease.update_info(credentials={})
        assert doctor.check_credentials(cfg)["status"] == "fail"  # a var not reported: missing
    finally:
        lease.release()
    assert SECRET not in json.dumps(c)


def test_doctor_cursor_ok_when_the_runner_holds_the_key(tmp_path, monkeypatch):
    _repo, cfg = _setup(tmp_path)
    _cursor(cfg)
    monkeypatch.setattr(RP, "probe_cursor_sdk",
                        lambda environ=None: {"ok": False, "error": "CURSOR_API_KEY is not set"})
    monkeypatch.setattr("nightwright.agentic.runner.cursor_needed", lambda cfg: True)
    with _serving_lease(cfg) as lease:
        lease.update_info(credentials={"CURSOR_API_KEY": "set"})
        c = doctor.check_cursor_agent(cfg)
    assert c["status"] == "ok" and "the runner holds CURSOR_API_KEY" in c["summary"]
    assert "CURSOR_API_KEY" not in os.environ


def test_doctor_registers_the_check_after_the_runner():
    import inspect
    src = inspect.getsource(doctor.run)
    runner = src.index('_safe("agentic.runner"')
    assert src.index('_safe("agentic.credentials", check_credentials') > runner
    assert src.index('_safe("agentic.credentials"') < src.index('_safe("agentic.cursor"')


# ---- the runner publishes ------------------------------------------------------------------

def test_tick_publishes_credentials_and_services_jobs(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path)
    cfg["agentic"]["credentials"] = {"NW_TEST_ONLY_TOKEN": "env"}
    monkeypatch.delenv("NW_TEST_ONLY_TOKEN", raising=False)
    served = []
    monkeypatch.setattr(Q.RNJ, "service", lambda cfg, stop=None, **kw: served.append(stop) or [])
    with LS.acquire(cfg, ["nw", "queue", "run"]) as lease:
        Q.tick(cfg, lease=lease)
        st = LS.lease_status(cfg)
    assert st["credentials"] == {"NW_TEST_ONLY_TOKEN": "missing"}
    assert served

