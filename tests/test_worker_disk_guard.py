"""SPEC-0388: worker disk guard. Offline, everything under tmp_path; never the real box."""
import json
import os
import threading
import time
from collections import namedtuple

import pytest
from click.testing import CliRunner

from nightwright.agentic import gates as G
from nightwright.agentic import worker as W
from nightwright.agentic import worker_job as WJ
from nightwright.agentic import workers as WK
from nightwright.agentic import workers_update as WU
from nightwright.cli import cli
from test_worker import _gate, _header


def _cfg(tmp_path, **workers):
    return {"data_dir": str(tmp_path / "data"), "agentic": {"workers": workers}}


def _job(cfg, name, size=10, age_s=3600, scratch=("tree", "tmp", "out")):
    job = W.jobs_dir(cfg) / name
    for d in scratch:
        (job / d).mkdir(parents=True)
        (job / d / "f").write_bytes(b"x" * size)
    for n in ("job.json", "frames.jsonl", "result.json"):
        (job / n).write_text("{}")
    old = time.time() - age_s
    for n in ("job.json", "frames.jsonl", "result.json"):
        os.utime(job / n, (old, old))
    return job


def test_config_parsing(tmp_path):
    assert W.abort_free_gb({}) == 2.0 and W.reap_lost_after_min({}) == 10.0
    assert W.abort_free_gb(_cfg(tmp_path, abort_free_gb=3)) == 3.0
    assert W.abort_free_gb(_cfg(tmp_path, abort_free_gb="x")) == 2.0
    assert W.reap_lost_after_min(_cfg(tmp_path, reap_lost_after_min=-1)) == 10.0
    assert W.reap_lost_after_min(_cfg(tmp_path, reap_lost_after_min=0)) == 0.0


def test_reap_removes_scratch_of_dead_old_jobs_only(tmp_path):
    cfg = _cfg(tmp_path)
    old = _job(cfg, "old", age_s=3600)
    young = _job(cfg, "young", age_s=60)
    live = _job(cfg, "live", age_s=3600)
    cache = W.repos_dir(cfg) / "r.git"
    cache.mkdir(parents=True)
    (cache / "obj").write_text("keep")
    other = W.worker_dir(cfg) / "cargo-target"
    other.mkdir(parents=True, exist_ok=True)
    (other / "x").write_text("keep")
    fd = os.open(live / "job.lock", os.O_RDWR | os.O_CREAT)
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_EX)
    # a lock held by *another* open file description reads as live in this process too
    try:
        assert W.reap_dead_scratch(cfg) == ["old"]
    finally:
        os.close(fd)
    assert not any((old / d).exists() for d in W.JOB_SCRATCH)
    assert (old / "job.json").exists() and (old / "frames.jsonl").exists()
    assert (old / "reaped_scratch.json").exists()
    assert (young / "tree").exists() and (live / "tree").exists()
    assert (cache / "obj").exists() and (other / "x").exists()


def test_reap_grace_zero_and_gc_runs_it(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, reap_lost_after_min=0)
    job = _job(cfg, "j", age_s=0)
    W.gc(cfg)
    assert not (job / "tree").exists() and (job / "job.json").exists()


def test_reap_skips_job_with_live_process_group(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    job = _job(cfg, "j")
    (job / "proc.json").write_text(json.dumps({"pid": 1, "gates": []}))
    monkeypatch.setattr("nightwright.agentic.runproc.group_alive", lambda g: True)
    assert W.reap_dead_scratch(cfg) == [] and (job / "tree").exists()


def _free(monkeypatch, gb):
    monkeypatch.setattr(W, "_free_bytes", lambda cfg: int(gb * 2**30))


def test_guard_kills_only_the_largest_live_job(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    big = _job(cfg, "big", size=5000)
    small = _job(cfg, "small", size=10)
    import fcntl
    fds = []
    for j in (big, small):
        fd = os.open(j / "job.lock", os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX)
        fds.append(fd)
    _free(monkeypatch, 1.0)
    terminated = []
    monkeypatch.setattr(G, "terminate_all", lambda: terminated.append(1))
    cb, cs = threading.Event(), threading.Event()
    gs, gb_ = W.DiskGuard(cfg, small, cs), W.DiskGuard(cfg, big, cb)
    gs.tick()
    assert gs.aborted is None and not cs.is_set()
    gb_.tick()
    assert "abort_free_gb" in gb_.aborted and cb.is_set() and terminated
    # enough free space, or the watch off: nothing happens
    _free(monkeypatch, 3.0)
    g2 = W.DiskGuard(cfg, big, threading.Event())
    g2.tick()
    assert g2.aborted is None
    _free(monkeypatch, 0.1)
    g3 = W.DiskGuard(_cfg(tmp_path, abort_free_gb=0), big, threading.Event())
    g3.tick()
    assert g3.aborted is None
    for fd in fds:
        os.close(fd)


def test_aborted_job_ends_with_disk_error_frame(tmp_path, monkeypatch):
    cfg = {"data_dir": str(tmp_path / "data")}
    job = tmp_path / "job"
    for d in ("tmp", "out", "tree"):
        (job / d).mkdir(parents=True)
    h = _header("j-1", "a" * 40, "b" * 40, [_gate("unit", "true")], bundle_bytes=0)
    (job / "header.json").write_text(json.dumps(h))
    (job / "job.json").write_text("{}")
    monkeypatch.setattr(W, "_checkout", lambda *a, **k: None)
    monkeypatch.setattr(W, "_start_gc", lambda cfg: None)
    monkeypatch.setattr(W, "_cleanup", lambda *a, **k: None)
    monkeypatch.setattr(W, "DISK_CHECK_S", 0.05)
    monkeypatch.setattr(W, "_free_bytes", lambda cfg: 1 * 2**30)
    monkeypatch.setattr(W, "_largest_live_job", lambda cfg: "job")

    def slow_gate(gate, tree, env, log, ph, log_rel, stop, deadline):
        assert stop.wait(10), "the guard never cancelled"
        return G.GateResult(name=gate["name"], required=True, ok=False, exit_code=None,
                            timed_out=False, duration_s=0.0, log="", tail="", interrupted=True)
    monkeypatch.setattr(G, "run_gate_with_retries", slow_gate)
    assert WJ.run_job(cfg, job, {}) != 0
    frames = [json.loads(ln) for ln in (job / "frames.jsonl").read_text().splitlines()]
    err = frames[-1]
    assert err["type"] == "error" and err["stage"] == "disk" and err["job_error"] is True
    assert "abort_free_gb" in err["error"] and err["disk"]["free"] >= 0
    assert "done" not in [f["type"] for f in frames]


def test_aborted_job_reports_disk_when_the_gate_raises(tmp_path, monkeypatch):
    """The guard has already aborted when the gate call raises. The error frame is still
    `stage: disk`, not `gate:unit`."""
    cfg = {"data_dir": str(tmp_path / "data")}
    job = tmp_path / "job"
    for d in ("tmp", "out", "tree"):
        (job / d).mkdir(parents=True)
    h = _header("j-1", "a" * 40, "b" * 40, [_gate("unit", "true")], bundle_bytes=0)
    (job / "header.json").write_text(json.dumps(h))
    (job / "job.json").write_text("{}")
    monkeypatch.setattr(W, "_checkout", lambda *a, **k: None)
    monkeypatch.setattr(W, "_start_gc", lambda cfg: None)
    monkeypatch.setattr(W, "_cleanup", lambda *a, **k: None)
    monkeypatch.setattr(W, "DISK_CHECK_S", 0.05)
    monkeypatch.setattr(W, "_free_bytes", lambda cfg: 1 * 2**30)
    monkeypatch.setattr(W, "_largest_live_job", lambda cfg: "job")

    def raising_gate(gate, tree, env, log, ph, log_rel, stop, deadline):
        assert stop.wait(10), "the guard never cancelled"
        raise RuntimeError("gate blew up after the kill")
    monkeypatch.setattr(G, "run_gate_with_retries", raising_gate)
    try:
        assert WJ.run_job(cfg, job, {}) != 0
    finally:
        G.reset()                          # the abort called terminate_all; later tests start clean
    frames = [json.loads(ln) for ln in (job / "frames.jsonl").read_text().splitlines()]
    err = frames[-1]
    assert err["type"] == "error" and err["stage"] == "disk" and err["job_error"] is True
    assert "abort_free_gb" in err["error"] and "DiskAbort" in err["error"]
    assert "gate blew up" not in err["error"]
    assert "done" not in [f["type"] for f in frames]


def test_jobs_report_sizes(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _job(cfg, "j", size=1000)
    rows = WJ.jobs(cfg)
    assert rows[0]["scratch_bytes"] == 3000 and rows[0]["size_bytes"] >= 3000
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["worker", "jobs", "--json"])
    assert r.exit_code == 0 and json.loads(r.output)[0]["size_bytes"] >= 3000
    r = CliRunner().invoke(cli, ["worker", "jobs"])
    assert r.output.split("\t")[0] == "j" and "KiB" in r.output


def test_update_preflight_blocks_low_free_space(tmp_path, monkeypatch):
    plan = WU.Plan("box", [], "w.whl", 100 * 2**20, "s", "b", b"", False)
    for free, ok in ((299 * 2**20, False), (300 * 2**20, True), (None, True)):
        info = {} if free is None else {"free_bytes": free}
        monkeypatch.setattr(WK, "check", lambda cfg, rec, info=info: {"info": info})
        if ok:
            WU._preflight({}, {}, plan)
        else:
            with pytest.raises(WU.UpdateError, match="not enough free disk"):
                WU._preflight({}, {}, plan)
    monkeypatch.setattr(WK, "check", lambda cfg, rec: (_ for _ in ()).throw(OSError("x")))
    WU._preflight({}, {}, plan)
