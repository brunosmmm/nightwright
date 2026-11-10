"""SPEC-0503: the worker's executor seam. A recording executor registered under a test name proves
submit / cancel / jobs (state) / host_slot / run_inline delegate to it; an unknown name is
refused with `stage: "executor"` before any claim; `nw worker run-job` reruns a job dir as a new
attempt from fresh scratch."""
import io
import json
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from nightwright.agentic import executors as EX
from nightwright.agentic import worker as W
from nightwright.agentic import worker_job as WJ
from test_worker import (_bundle_job, _gate, _header, _job_dir, _no_finish_gc,  # noqa: F401
                         src, wenv)
from waits import until

pytestmark = pytest.mark.integration


def _cfg(env, **workers):
    return {"data_dir": env["NW_DATA_DIR"], "config_dir": env["NW_CONFIG_DIR"],
            "agentic": {"workers": {"min_free_gb": 0, "abort_free_gb": 0, **workers}}}


def _read(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _frames(job):
    return [json.loads(ln) for ln in (job / "frames.jsonl").read_text().splitlines() if ln]


class Recorder(EX.LocalExecutor):
    """The local executor, with every seam call recorded."""

    name = "rec"
    calls: list = []

    def start(self, cfg, job, h, lock_fd):
        self.calls.append("start")
        return super().start(cfg, job, h, lock_fd)

    def cancel(self, cfg, job, rec):
        self.calls.append("cancel")
        return super().cancel(cfg, job, rec)

    def state(self, cfg, job, rec, sett, now):
        self.calls.append("state")
        return super().state(cfg, job, rec, sett, now)

    @contextmanager
    def host_slot(self, cfg, h, cancel, announce):
        self.calls.append("host_slot")
        with super().host_slot(cfg, h, cancel, announce) as hold:
            yield hold

    def run_inline(self, cfg, argv, stdin, stdout):
        self.calls.append(("run_inline", tuple(argv)))
        return super().run_inline(cfg, argv, stdin, stdout)


@pytest.fixture
def rec():
    Recorder.calls = []
    EX.register("rec", Recorder)
    yield Recorder.calls
    EX.unregister("rec")


def _line(h, data=b""):
    return io.BytesIO((json.dumps(h) + "\n").encode() + data)


# ---- an unknown executor -----------------------------------------------------------------------

def test_unknown_executor_refuses_submit_and_claims_nothing(wenv):
    h = _header("j-unknown", "a" * 40, "b" * 40, [_gate("x", "true")], bundle_bytes=0)
    out = io.BytesIO()
    assert WJ.submit(_cfg(wenv, executor="nope"), _line(h), out) == W.EXIT_ERROR
    (err,) = [json.loads(ln) for ln in out.getvalue().splitlines()]
    assert err["type"] == "error" and err["stage"] == "executor" and "nope" in err["error"]
    assert not _job_dir(wenv, "j-unknown").exists()


def test_unknown_executor_refuses_gate_job_and_claims_nothing(wenv):
    h = _header("j-unknown2", "a" * 40, "b" * 40, [_gate("x", "true")], bundle_bytes=0)
    out = io.BytesIO()
    assert W.gate_job(_cfg(wenv, executor="nope"), _line(h), out) == W.EXIT_ERROR
    (err,) = [json.loads(ln) for ln in out.getvalue().splitlines()]
    assert err["type"] == "error" and err["stage"] == "executor"
    assert err["job_id"] == "j-unknown2"
    assert not _job_dir(wenv, "j-unknown2").exists()


def test_info_reports_the_executor(wenv):
    info = W.info(_cfg(wenv))
    assert info["executor"] == "local" and "executor:local" in info["features"]
    bad = W.info(_cfg(wenv, executor="nope"))
    assert bad["executor"] == "nope" and "executor_error" in bad
    assert not any(f.startswith("executor:") for f in bad["features"])


# ---- delegation --------------------------------------------------------------------------------

def test_submit_jobs_and_cancel_go_through_the_executor(src, tmp_path, wenv, rec):
    marker = tmp_path / "never"
    h, data = _bundle_job(src, tmp_path, [_gate(
        "wait", f'while [ ! -f "{marker}" ]; do sleep 0.05; done')])
    cfg = _cfg(wenv, executor="rec")
    out = io.BytesIO()
    assert WJ.submit(cfg, _line(h, data), out) == 0, out.getvalue()
    assert rec == ["start"]
    job = _job_dir(wenv, h["job_id"])
    assert _read(job / "job.json")["executor"] == "rec"
    assert set(_read(job / "spawn.json")) >= {"pid", "pgid", "start_ticks", "spawned_at"}
    assert until(lambda: any(f["type"] == "started" for f in _frames(job))
                 if (job / "frames.jsonl").exists() else False)
    rows = {r["job_id"]: r for r in WJ.jobs(cfg)}
    assert rows[h["job_id"]]["state"] == "running" and "state" in rec
    out = io.BytesIO()
    assert WJ.cancel(cfg, h["job_id"], out) == 0
    assert "cancel" in rec and (job / "cancel").exists()
    assert until(lambda: _read(job / "result.json") is not None)
    done = _frames(job)[-1]
    assert done["type"] == "done" and done["interrupted"] is True
    assert _read(job / "result.json")["attempt"] == 1


def test_gate_job_runs_inline_through_the_executor(src, tmp_path, wenv, rec):
    h, data = _bundle_job(src, tmp_path, [_gate("hello", "echo hi")])
    out = io.BytesIO()
    r, w = os.pipe()                            # stdin stays open (EOF would be a cancel)
    payload = (json.dumps(h) + "\n").encode() + data
    threading.Thread(target=lambda: os.write(w, payload), daemon=True).start()
    try:
        with os.fdopen(r, "rb") as stdin:
            assert W.gate_job(_cfg(wenv, executor="rec"), stdin, out) == 0
    finally:
        os.close(w)
    assert rec[0] == ("run_inline", ("gate-job",))
    types = [json.loads(ln)["type"] for ln in out.getvalue().splitlines()]
    assert types == ["accepted", "started", "gate", "done"]


def test_run_gates_takes_its_host_slot_through_the_executor(wenv, rec, tmp_path):
    cfg = _cfg(wenv, executor="rec")
    job = W.jobs_dir(cfg) / "j-slot"
    job.mkdir(parents=True)
    (job / "job.json").write_text(json.dumps({"executor": "rec"}))
    h = _header("j-slot", "a" * 40, "b" * 40, [], bundle_bytes=0)
    out = io.BytesIO()
    frames = W._Frames("j-slot", out)
    W._run_gates(cfg, h, job, frames, threading.Event())
    assert rec == ["host_slot"]
    assert [json.loads(ln)["type"] for ln in out.getvalue().splitlines()] == ["started", "done"]


# ---- run-job: a rerun is a new attempt ---------------------------------------------------------

def _run_job(env, job, attempt=None):
    e = dict(env)
    if attempt is not None:
        e[WJ.ATTEMPT_ENV] = str(attempt)
    return subprocess.run([sys.executable, "-m", "nightwright", "worker", "run-job", str(job)],
                          env=e, capture_output=True, text=True, timeout=120)


def test_run_job_reruns_a_finished_job_from_fresh_scratch(src, tmp_path, wenv):
    h, data = _bundle_job(src, tmp_path, [_gate("hello", "echo attempt-$NW_SPEC_ID")])
    cfg = _cfg(wenv)
    out = io.BytesIO()
    assert WJ.submit(cfg, _line(h, data), out) == 0
    job = _job_dir(wenv, h["job_id"])
    assert until(lambda: _read(job / "result.json") is not None)
    first = _frames(job)
    assert [f["type"] for f in first] == ["accepted", "started", "gate", "done"]
    assert _read(job / "result.json")["attempt"] == 1
    # leftovers a fresh attempt must not see (a stale `tree/` would make the clone fail)
    for sub in ("tree", "out", "tmp"):
        (job / sub).mkdir(exist_ok=True)
        (job / sub / "stale").write_text("x")
    p = _run_job(wenv, job, attempt=2)
    assert p.returncode == 0, p.stderr
    frames = _frames(job)
    assert frames[:4] == first
    rest = frames[4:]
    assert [f["type"] for f in rest] == ["attempt", "accepted", "started", "gate", "done"]
    assert rest[0]["attempt"] == 2 and rest[0]["n"] == 5
    assert [f["n"] for f in rest] == [5, 6, 7, 8, 9]
    assert rest[-1]["ok"] is True
    res = _read(job / "result.json")
    assert res["attempt"] == 2 and res["state"] == "done"
    assert _read(job / "job.json")["run_attempts"] == 2
    assert _read(job / "proc.json")["attempt"] == 2
    # a run of an older attempt never touches the newer result
    p = _run_job(wenv, job, attempt=1)
    assert p.returncode == W.EXIT_ERROR and "already has a result" in p.stderr
    assert _read(job / "result.json")["attempt"] == 2


def test_attempt_numbering(wenv, tmp_path, monkeypatch):
    job = tmp_path / "j"
    job.mkdir()
    monkeypatch.delenv(WJ.ATTEMPT_ENV, raising=False)
    assert WJ._attempt(job) == 1
    (job / "job.json").write_text(json.dumps({"run_attempts": 2}))
    assert WJ._attempt(job) == 3
    monkeypatch.setenv(WJ.ATTEMPT_ENV, "7")
    assert WJ._attempt(job) == 7
    assert WJ._result_attempt({}) == 1 and WJ._result_attempt({"attempt": 4}) == 4


def test_lease_expiry_cancels_only_when_the_executor_says_so(tmp_path):
    job = tmp_path / "j"
    job.mkdir()
    (job / "lease.json").write_text(json.dumps({"lease_until": 1.0}))     # long expired
    for lease_cancels, expect in ((False, False), (True, True)):
        cancel, stop = threading.Event(), threading.Event()
        t = threading.Thread(target=WJ._watch, args=(job, {}, cancel, stop, lease_cancels),
                             daemon=True)
        t.start()
        if expect:
            assert until(cancel.is_set)
        else:
            # the watch ticks every WATCH_TICK_S; a few ticks with no cancel is the claim
            assert not cancel.wait(WJ.WATCH_TICK_S * 4)
        stop.set()
        t.join(timeout=5)
    from nightwright.agentic import gates as G
    G.reset()


def test_the_default_executor_is_local():
    assert EX.configured({}) == "local"
    assert EX.get({}).name == "local" and EX.get({}).lease_cancels is True
    assert EX.for_job({}, Path("/nonexistent")).name == "local"
