"""SPEC-0340: worker-owned async jobs (`nw worker submit|attach|poll|cancel|jobs`), offline. Every
call goes through `tests/remote_loopback.py` (the forced command, run locally in place of
`ssh`), except where a timing bound is checked in-process."""
import hashlib
import io
import json
import os
import shutil
import signal
import threading
import time
from pathlib import Path

import pytest
import yaml

import remote_loopback as LB
from nightwright.agentic import worker as W
from nightwright.agentic import worker_job as WJ
from test_worker import (Job, Src, _alive, _bundle_job, _gate, _header, _job_dir,  # noqa: F401
                         _no_finish_gc, _sleep_pid, _sleeper, src, wenv)
from waits import WAIT_S, until


@pytest.fixture(autouse=True)
def _kill_leftover_jobs(tmp_path):
    """A failing test must not leave a detached job process (or its gates) running."""
    yield
    jd = W.jobs_dir({"data_dir": str(tmp_path / "wdata")})        # `wenv`'s data dir
    for proc in jd.glob("*/proc.json") if jd.is_dir() else []:
        rec = _read(proc) or {}
        for g in [rec, *(rec.get("gates") or [])]:
            # never this test process's own group (`_fake_job` records it)
            if g.get("pgid") and g["pgid"] != os.getpgid(0) and g.get("pid") != os.getpid():
                WJ._kill_group({"pid": g.get("pid"), "pgid": g["pgid"],
                                "start_ticks": g.get("start_ticks")}, 0.2, cont=True)


def _conf(env, **workers):
    (Path(env["NW_CONFIG_DIR"]) / "config.yaml").write_text(
        yaml.safe_dump({"agentic": {"workers": workers}}))
    return {**env, WJ.KILL_GRACE_ENV: "1"}


def _cfg(env, **workers):
    cfg = {"data_dir": env["NW_DATA_DIR"], "config_dir": env["NW_CONFIG_DIR"]}
    if workers:
        cfg["agentic"] = {"workers": workers}
    return cfg


def _recs(result):
    return [json.loads(ln) for ln in result.lines]


def _submit(env, h, data=b""):
    return LB.run(env, "nw worker submit", (json.dumps(h) + "\n").encode() + data)


def _attach(env, job_id, after=0, **kw):
    return LB.run(env, f"nw worker attach {job_id} --from {after}", **kw)


def _poll(env, job_id, after=0, renew=False):
    r = LB.run(env, f"nw worker poll {job_id} --from {after}" + (" --renew" if renew else ""))
    assert r.returncode == 0, r.stderr
    return _recs(r)


def _state_in_process(env, job_id, **workers):
    out = io.BytesIO()
    WJ.poll(_cfg(env, **workers), job_id, 0, False, out)
    return json.loads(out.getvalue().splitlines()[-1])


def _read(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def _result(env, job_id):
    p = _job_dir(env, job_id) / "result.json"
    assert until(lambda: _read(p) is not None), (_job_dir(env, job_id) / "stderr.log").read_text()
    return _read(p)


def _proc(env, job_id, gates=False):
    p = _job_dir(env, job_id) / "proc.json"
    assert until(lambda: (_read(p) or {}).get("pid") and (not gates or _read(p)["gates"]))
    return _read(p)


def _wait_file(path):
    return f'while [ ! -f "{path}" ]; do sleep 0.05; done; echo released'


def _nw_jobs(env):
    r = LB.run(env, "nw worker jobs --json")
    assert r.returncode == 0, r.stderr
    return {j["job_id"]: j for j in json.loads(b"\n".join(r.lines))}


# ---- submit / attach -------------------------------------------------------------------------

def test_submit_returns_before_the_gates_and_attach_follows_to_done(src, tmp_path, wenv):
    marker = tmp_path / "go"
    h, data = _bundle_job(src, tmp_path, [_gate("wait", _wait_file(marker))])
    t0 = time.time()
    r = _submit(wenv, h, data)
    assert r.returncode == 0, r.stderr
    (status,) = _recs(r)
    assert status["type"] == "status" and status["state"] == "accepted" and status["n"] == 0
    assert status["job_id"] == h["job_id"]
    job = _job_dir(wenv, h["job_id"])
    lease = _read(job / "lease.json")
    assert abs(lease["lease_until"] - (t0 + WJ.DEFAULTS["job_lease_s"])) < 30
    rec = _read(job / "job.json")
    expect = hashlib.sha256(json.dumps(h, sort_keys=True, separators=(",", ":"),
                                       ensure_ascii=False).encode()).hexdigest()
    assert rec["header_digest"] == expect == WJ.header_digest(h)
    assert not (job / "result.json").exists()           # the gate is still waiting
    marker.touch()
    a = _attach(wenv, h["job_id"])
    assert a.returncode == 0, a.stderr
    frames = _recs(a)
    assert [f["type"] for f in frames] == ["accepted", "started", "gate", "done"]
    assert [f["n"] for f in frames] == [1, 2, 3, 4]
    assert frames[-1]["ok"] is True and frames[-1]["interrupted"] is False
    assert _result(wenv, h["job_id"])["state"] == "done"
    assert until(lambda: _read(job / "job.json")["state"] == "done")


def test_resubmit_same_header_is_idempotent_and_a_different_one_is_refused(src, tmp_path, wenv):
    marker = tmp_path / "go"
    h, data = _bundle_job(src, tmp_path, [_gate("wait", _wait_file(marker))])
    assert _submit(wenv, h, data).returncode == 0
    pid = _proc(wenv, h["job_id"])["pid"]
    again = _submit(wenv, h, data)
    assert again.returncode == 0, again.stderr
    (st,) = _recs(again)
    assert st["type"] == "status" and st["state"] in ("accepted", "running")
    other = _submit(wenv, {**h, "attempt": 1}, data)
    assert other.returncode == 3
    (err,) = _recs(other)
    assert err["type"] == "error" and err["stage"] == "claim"
    marker.touch()
    assert _result(wenv, h["job_id"])["state"] == "done"
    frames = _poll(wenv, h["job_id"])[:-1]
    # one job process: one claim, one run
    assert [f["type"] for f in frames].count("accepted") == 1
    assert [f["type"] for f in frames].count("done") == 1
    assert _read(_job_dir(wenv, h["job_id"]) / "proc.json")["pid"] == pid


def test_attach_after_a_checkout_error_exits_3(src, tmp_path, wenv):
    # no bundle and an empty cache: the job process's checkout fails
    h = _header("j-err", src.head, src.base, [_gate("x", "true")], bundle_bytes=0,
                  repo_key="empty")
    assert _submit(wenv, h).returncode == 0
    a = _attach(wenv, "j-err")
    assert a.returncode == 3, a.stderr
    (err,) = _recs(a)
    assert err["type"] == "error" and err["stage"] == "checkout" and err["n"] == 1
    assert _result(wenv, "j-err")["state"] == "error"
    assert _nw_jobs(wenv)["j-err"]["state"] == "error"
    c = LB.run(wenv, "nw worker cancel j-err")              # a no-op on a finished job
    assert c.returncode == 0 and _recs(c)[-1]["state"] == "error"
    assert _result(wenv, "j-err")["state"] == "error"


def test_reattach_after_a_dropped_connection_loses_and_repeats_nothing(src, tmp_path, wenv):
    marker = tmp_path / "go"
    h, data = _bundle_job(src, tmp_path, [_gate("one", "echo one"),
                                          _gate("two", _wait_file(marker))])
    assert _submit(wenv, h, data).returncode == 0
    first = _attach(wenv, h["job_id"], kill_after=3)      # accepted, started, gate one
    assert first.dropped
    seen = _recs(first)
    assert [f["n"] for f in seen] == [1, 2, 3]
    # an attach killed by SIGTERM never cancels the job (killed once it renewed: it is following)
    lease = _job_dir(wenv, h["job_id"]) / "lease.json"
    before = _read(lease)["lease_until"]
    p = LB.start(wenv, f"nw worker attach {h['job_id']} --from 3", b"")
    assert until(lambda: (_read(lease) or {}).get("lease_until", before) != before)
    p.terminate()
    p.wait(timeout=WAIT_S)
    assert _state_in_process(wenv, h["job_id"])["state"] == "running"
    marker.touch()
    rest = _attach(wenv, h["job_id"], after=seen[-1]["n"])
    assert rest.returncode == 0
    allf = seen + _recs(rest)
    assert [f["n"] for f in allf] == [1, 2, 3, 4, 5]
    assert [f["type"] for f in allf] == ["accepted", "started", "gate", "gate", "done"]
    assert allf[-1]["interrupted"] is False


def _fake_job(env, job_id):
    """An async job dir whose 'job process' is this test (alive, heartbeating once)."""
    job = W.jobs_dir(_cfg(env)) / job_id
    job.mkdir(parents=True)
    (job / "header.json").write_text("{}\n")
    (job / "job.json").write_text(json.dumps({"job_id": job_id, "state": "running",
                                              "header_digest": "x"}) + "\n")
    (job / "job.lock").touch()
    me = os.getpid()
    from nightwright.agentic import runproc
    (job / "proc.json").write_text(json.dumps(
        {"pid": me, "pgid": os.getpgid(0), "start_ticks": runproc.start_ticks(me),
         "started_ts": time.time(), "gates": []}))
    (job / "heartbeat.json").write_text(json.dumps({"ts": time.time() + 600, "pid": me}))
    return job


def _frame(job_id, n, type_="gate", **kw):
    return (json.dumps({"schema": W.FRAME_SCHEMA, "type": type_, "job_id": job_id, "n": n,
                        **kw}) + "\n").encode()


def test_a_partial_last_line_is_held_back_until_its_newline(wenv):
    wenv = _conf(wenv, job_lease_s=1.2)     # attach rewrites the lease every tick (0.4 s < 0.5)
    job = _fake_job(wenv, "j-part")
    second = _frame("j-part", 2, blob="x" * 100_000)
    with open(job / "frames.jsonl", "wb") as f:
        f.write(_frame("j-part", 1, "accepted") + second[:50_000])
    recs = _poll(wenv, "j-part")
    assert [r.get("n") for r in recs] == [1, 1]           # frame 1, then the status (n: 1)
    assert recs[-1]["type"] == "status" and recs[-1]["state"] == "running"
    p = LB.start(wenv, "nw worker attach j-part --from 0", b"")
    lines = []
    t = threading.Thread(target=lambda: lines.extend(p.stdout), daemon=True)
    t.start()
    assert until(lambda: len(lines) == 1)
    seen = set()

    def ticks():
        seen.add((_read(job / "lease.json") or {}).get("lease_until"))
        return len(seen - {None}) >= 3
    assert until(ticks)                                   # two more follow ticks went by
    assert len(lines) == 1
    with open(job / "frames.jsonl", "ab") as f:
        f.write(second[50_000:])
    assert until(lambda: len(lines) == 2)
    assert json.loads(lines[1]) == json.loads(second)
    with open(job / "frames.jsonl", "ab") as f:
        f.write(_frame("j-part", 3, "done", ok=True))
    assert p.wait(timeout=WAIT_S) == 0
    t.join(WAIT_S)
    assert [json.loads(ln)["n"] for ln in lines] == [1, 2, 3]


# ---- the lease -------------------------------------------------------------------------------

def test_a_job_whose_lease_is_not_renewed_cancels_itself(src, tmp_path, wenv):
    env = _conf(wenv, job_lease_s=2)
    pidfile = tmp_path / "sleep.pid"
    h, data = _bundle_job(src, tmp_path, [_sleeper(pidfile)])
    # a long first lease, so a slow checkout on a loaded machine cannot spend the 2 s one
    # before the gate starts; then one 2 s renewal and none after it
    assert WJ.submit(_cfg(env, job_lease_s=600),
                     io.BytesIO((json.dumps(h) + "\n").encode() + data), io.BytesIO()) == 0
    pid = _sleep_pid(pidfile)
    WJ._renew(_job_dir(env, h["job_id"]), 2)
    res = _result(env, h["job_id"])
    assert res["state"] == "done" and res["frame"]["interrupted"] is True
    assert until(lambda: not _alive(pid))


def test_a_long_silent_gate_is_never_cancelled_while_attached(src, tmp_path, wenv):
    env = _conf(wenv, job_lease_s=2)
    h, data = _bundle_job(src, tmp_path, [_gate("long", "sleep 5; echo ok")])
    lease = _job_dir(env, h["job_id"]) / "lease.json"
    changes, lapsed, stop = [], [], threading.Event()

    def watch():
        last = None
        while not stop.is_set():
            rec = _read(lease)
            if rec:
                if rec["lease_until"] != last:
                    last = rec["lease_until"]
                    changes.append(time.monotonic())
                if rec["lease_until"] < time.time():
                    lapsed.append(rec)
            time.sleep(0.02)

    out = io.BytesIO()
    assert _submit(env, h, data).returncode == 0
    w = threading.Thread(target=watch, daemon=True)
    w.start()
    try:
        # in-process: the 2 s lease is not spent on a second interpreter's startup
        rc = WJ.attach(_cfg(env, job_lease_s=2), h["job_id"], 0, out)
    finally:
        stop.set()
        w.join(WAIT_S)
    assert rc == 0, (_job_dir(env, h["job_id"]) / "stderr.log").read_text()
    frames = [json.loads(ln) for ln in out.getvalue().splitlines()]
    assert frames[-1]["type"] == "done"
    assert frames[-1]["ok"] is True and frames[-1]["interrupted"] is False
    assert not lapsed
    attached = changes[1:]                  # changes[0] is submit's lease, seen first
    gaps = [b - a for a, b in zip(attached, attached[1:])]
    # min(30, 2/3) s apart, give or take a follow tick (plus one for a loaded machine);
    # `not lapsed` above is the hard bound
    assert gaps and max(gaps) <= 2 / 3 + 2 * WJ.FOLLOW_TICK_S, gaps
    assert min(gaps) >= 2 / 3 - 0.05, gaps                # throttled: never every tick


class _Clock:
    """`worker_job`'s `time` for one attach: `sleep` advances a fake monotonic clock (wall time
    stays real), and once it reaches `stop_at` the job's `done` frame appears."""

    def __init__(self, job, stop_at):
        self.job, self.stop_at, self.t = job, stop_at, 0.0
        self.time = time.time

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s
        if self.t >= self.stop_at and not (self.job / "frames.jsonl").exists():
            (self.job / "frames.jsonl").write_bytes(_frame(self.job.name, 1, "done", ok=True))


@pytest.mark.parametrize("lease_s, every", [(300, 30.0), (900, 30.0), (60, 20.0), (2, 2 / 3)])
def test_attach_rewrites_the_lease_at_most_every_third_of_it(wenv, monkeypatch, lease_s, every):
    job = _fake_job(wenv, "j-clk")
    clock = _Clock(job, stop_at=3 * every + 2)
    renews, real = [], WJ._renew
    monkeypatch.setattr(WJ, "time", clock)
    monkeypatch.setattr(WJ, "_renew", lambda j, s: (renews.append(clock.t), real(j, s)))
    assert WJ.attach(_cfg(wenv, job_lease_s=lease_s), "j-clk", 0, io.BytesIO()) == 0
    gaps = [b - a for a, b in zip(renews, renews[1:])]
    # renewed on the first tick, then once more than `every` passed: one tick late at most
    assert renews[0] == 0 and len(gaps) == 3, renews
    assert all(every < g <= every + WJ.FOLLOW_TICK_S for g in gaps), renews


def test_settings_defaults_and_the_heartbeat_cap():
    s = WJ.settings({"agentic": {"workers": {"job_lease_s": 2, "job_heartbeat_s": 0.5,
                                             "job_lost_after_s": "bad"}}})
    assert s == {"job_lease_s": 2.0, "job_heartbeat_s": 0.5, "job_lost_after_s": 60.0}
    assert WJ.settings({}) == {"job_lease_s": 900.0, "job_heartbeat_s": 10.0,
                               "job_lost_after_s": 60.0}
    # a heartbeat as slow as the lost bound would make a healthy job look hung
    for hb in (60, 120):
        s = WJ.settings({"agentic": {"workers": {"job_heartbeat_s": hb}}})
        assert s["job_heartbeat_s"] == 20.0 and s["job_lost_after_s"] == 60.0


# ---- lost jobs -------------------------------------------------------------------------------

LOST = {"job_lost_after_s": 2, "job_heartbeat_s": 0.5}


def _hung_job(src, tmp_path, env, name):
    pidfile = tmp_path / f"{name}.pid"
    h, data = _bundle_job(src, tmp_path, [_sleeper(pidfile)], job_id=name)
    assert _submit(env, h, data).returncode == 0
    sleep_pid = _sleep_pid(pidfile)
    proc = _proc(env, name, gates=True)
    os.kill(proc["pid"], signal.SIGSTOP)
    t0 = time.monotonic()
    assert until(lambda: _state_in_process(env, name, **LOST)["state"] == "lost", timeout=3.0)
    assert time.monotonic() - t0 <= 3.0
    assert _poll(env, name)[-1]["state"] == "lost"
    return proc, sleep_pid


def test_cancel_on_a_hung_job_kills_its_groups_and_records_lost(src, tmp_path, wenv):
    env = _conf(wenv, **LOST)
    proc, sleep_pid = _hung_job(src, tmp_path, env, "j-hung")
    r = LB.run(env, "nw worker cancel j-hung")
    assert r.returncode == 0, r.stderr
    assert _recs(r)[-1]["state"] == "lost"
    assert until(lambda: not _alive(proc["pid"]) and not _alive(sleep_pid))
    assert all(not _alive(g["pid"]) for g in proc["gates"])
    assert _read(_job_dir(env, "j-hung") / "result.json")["state"] == "lost"
    assert _read(_job_dir(env, "j-hung") / "job.json")["state"] == "lost"


def test_gc_reaps_a_hung_job(src, tmp_path, wenv):
    env = _conf(wenv, **LOST)
    proc, sleep_pid = _hung_job(src, tmp_path, env, "j-hung2")
    r = LB.run(env, "nw worker gc")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.lines[0])["jobs_reaped"] == ["j-hung2"]
    assert until(lambda: not _alive(proc["pid"]) and not _alive(sleep_pid))
    assert _read(_job_dir(env, "j-hung2") / "result.json")["state"] == "lost"


def test_a_dead_job_process_is_lost_and_attach_exits_4(src, tmp_path, wenv):
    env = _conf(wenv, job_lease_s=6)        # attach would rewrite the lease every 2 s
    pidfile = tmp_path / "sleep.pid"
    h, data = _bundle_job(src, tmp_path, [_sleeper(pidfile)])
    job = _job_dir(env, h["job_id"])
    assert _submit(env, h, data).returncode == 0
    p = LB.start(env, f"nw worker attach {h['job_id']} --from 0", b"")
    lines = []
    t = threading.Thread(target=lambda: lines.extend(p.stdout), daemon=True)
    t.start()
    sleep_pid = _sleep_pid(pidfile)
    assert until(lambda: any(json.loads(ln)["type"] == "started" for ln in lines))
    os.kill(_proc(env, h["job_id"])["pid"], signal.SIGKILL)
    t0 = time.monotonic()
    assert p.wait(timeout=WAIT_S) == 4
    assert time.monotonic() - t0 <= 2.0
    t.join(WAIT_S)
    last = json.loads(lines[-1])
    assert last["type"] == "status" and last["state"] == "lost" and "heartbeat_age_s" in last
    at_exit = _read(job / "lease.json")["lease_until"]
    time.sleep(2.5)                                     # over one 2 s rewrite interval
    assert _read(job / "lease.json")["lease_until"] == at_exit     # no renewal after the exit
    assert _poll(env, h["job_id"])[-1]["state"] == "lost"
    assert _read(job / "job.json")["state"] == "lost"   # job.json mirrors the derived state
    # the orphaned gate is reaped by cancel's kill path
    assert LB.run(env, f"nw worker cancel {h['job_id']}").returncode == 0
    assert until(lambda: not _alive(sleep_pid))


# ---- cancel ----------------------------------------------------------------------------------

def test_cancel_a_running_job_then_cancel_a_done_one(src, tmp_path, wenv):
    pidfile = tmp_path / "sleep.pid"
    h, data = _bundle_job(src, tmp_path, [_sleeper(pidfile)])
    assert _submit(wenv, h, data).returncode == 0
    pid = _sleep_pid(pidfile)
    job_pid = _proc(wenv, h["job_id"])["pid"]
    r = LB.run(wenv, f"nw worker cancel {h['job_id']}")
    assert r.returncode == 0 and _recs(r)[-1]["cancel"] == "requested"
    res = _result(wenv, h["job_id"])
    assert res["state"] == "done" and res["frame"]["interrupted"] is True
    assert until(lambda: not _alive(pid))
    # the job process itself ends too, and with it its hold on job.lock
    assert until(lambda: not _alive(job_pid))
    assert WJ._lock_held(_job_dir(wenv, h["job_id"])) is False
    again = LB.run(wenv, f"nw worker cancel {h['job_id']}")
    assert again.returncode == 0 and _recs(again)[-1]["state"] == "done"


# ---- gate-job keeps SPEC-0336's contract -----------------------------------------------------

def test_gate_job_exit_codes_and_repeated_job_id(src, tmp_path, wenv):
    h, data = _bundle_job(src, tmp_path, [_gate("x", "true")])
    job = Job(wenv, h, data)
    fin, rc = job.finish()
    assert fin["type"] == "done" and rc == 0
    assert [f["n"] for f in job.frames] == [1, 2, 3, 4]
    dup = Job(wenv, h, data)
    fin, rc = dup.finish()
    assert fin["type"] == "error" and fin["stage"] == "claim" and rc == 3
    bad = _header("j-gerr", src.head, src.base, [_gate("x", "true")], bundle_bytes=0,
                  repo_key="empty")
    fin, rc = Job(wenv, bad).finish()
    assert fin["type"] == "error" and rc == 3
    # a submitted job's id is refused by gate-job too (it never attaches)
    s, sdata = _bundle_job(src, tmp_path, [_gate("x", "true")])
    assert _submit(wenv, s, sdata).returncode == 0
    fin, rc = Job(wenv, s, sdata).finish()
    assert fin["stage"] == "claim" and rc == 3


@pytest.mark.parametrize("how", ["eof", "SIGHUP", "SIGTERM", "SIGINT"])
def test_gate_job_eof_and_signals_still_cancel(src, tmp_path, wenv, how):
    pidfile = tmp_path / "sleep.pid"
    h, data = _bundle_job(src, tmp_path, [_sleeper(pidfile)])
    job = Job(wenv, h, data)
    pid = _sleep_pid(pidfile)
    if how == "eof":
        job.close()
    else:
        job.proc.send_signal(getattr(signal, how))
    fin = job.final()
    assert fin["type"] == "done" and fin["interrupted"] is True
    job.close()
    assert job.proc.wait(timeout=WAIT_S) == 0
    assert until(lambda: not _alive(pid))


# ---- jobs and gc -----------------------------------------------------------------------------

def test_jobs_lists_every_state_and_gc_keeps_only_the_running_job(src, tmp_path, wenv):
    env = _conf(wenv, job_lease_s=600)
    done, ddata = _bundle_job(src, tmp_path, [_gate("x", "true")], job_id="j-done")
    assert _submit(env, done, ddata).returncode == 0
    assert _attach(env, "j-done").returncode == 0
    err = _header("j-error", src.head, src.base, [_gate("x", "true")], bundle_bytes=0,
                  repo_key="empty")
    assert _submit(env, err).returncode == 0
    assert _attach(env, "j-error").returncode == 3
    run_pid = tmp_path / "run.pid"
    run, rdata = _bundle_job(src, tmp_path, [_sleeper(run_pid)], job_id="j-run")
    assert _submit(env, run, rdata).returncode == 0
    lost_pid = tmp_path / "lost.pid"
    lost, ldata = _bundle_job(src, tmp_path, [_sleeper(lost_pid)], job_id="j-lost")
    assert _submit(env, lost, ldata).returncode == 0
    run_sleep, lost_sleep = _sleep_pid(run_pid), _sleep_pid(lost_pid)
    os.kill(_proc(env, "j-lost", gates=True)["pid"], signal.SIGKILL)
    acc = W.jobs_dir(_cfg(env)) / "j-acc"                 # claimed, process not started yet
    acc.mkdir()
    (acc / "header.json").write_text("{}\n")
    (acc / "job.json").write_text(json.dumps({"header_digest": "x",
                                              "spawned_ts": time.time() + 600}) + "\n")
    (acc / "job.lock").touch()
    assert until(lambda: _nw_jobs(env)["j-lost"]["state"] == "lost")
    states = {k: v["state"] for k, v in _nw_jobs(env).items()}
    assert states == {"j-done": "done", "j-error": "error", "j-run": "running",
                      "j-lost": "lost", "j-acc": "accepted"}
    assert set(WJ.STATES) == set(states.values())
    r = LB.run(env, "nw worker gc --keep-days 0")
    assert r.returncode == 0, r.stderr
    rec = json.loads(r.lines[0])
    assert {"j-done", "j-error", "j-lost"} <= set(rec["jobs_removed"])
    assert "j-run" not in rec["jobs_removed"] and _job_dir(env, "j-run").is_dir()
    assert until(lambda: not _alive(lost_sleep))           # the lost job's gate was reaped
    assert _alive(run_sleep)
    assert LB.run(env, "nw worker cancel j-run").returncode == 0
    assert _result(env, "j-run")["state"] == "done"
    assert until(lambda: not _alive(run_sleep))


# ---- ssh-command and info --------------------------------------------------------------------

@pytest.fixture
def execs(monkeypatch):
    calls = []
    monkeypatch.setattr(W.os, "execvp", lambda file, argv: calls.append(argv))
    return calls


@pytest.mark.parametrize("cmd, argv", [
    ("nw worker submit", ["submit"]),
    ("nw worker attach j-1.x_2", ["attach", "j-1.x_2"]),
    ("nw worker attach j-1 --from 0", ["attach", "j-1", "--from", "0"]),
    ("nw worker poll j-1 --from 12", ["poll", "j-1", "--from", "12"]),
    ("nw worker poll j-1 --from 12 --renew", ["poll", "j-1", "--from", "12", "--renew"]),
    ("nw worker poll j-1 --renew", ["poll", "j-1", "--renew"]),
    ("nw worker cancel j-1", ["cancel", "j-1"]),
    ("nw worker jobs", ["jobs"]),
    ("nw worker jobs --json", ["jobs", "--json"]),
])
def test_ssh_command_admits_the_async_subcommands(execs, cmd, argv):
    W.ssh_command({"SSH_ORIGINAL_COMMAND": cmd})
    assert [a[-len(argv):] for a in execs] == [argv]


@pytest.mark.parametrize("cmd", [
    "nw worker submit --force",
    "nw worker attach", "nw worker attach ../x", "nw worker attach .. --from 0",
    "nw worker attach j-1 --from -1", "nw worker attach j-1 --from 1x",
    "nw worker attach j-1 --from", "nw worker attach j-1 --renew",
    "nw worker attach 'j-1\n' --from 0", "nw worker attach j-1 --from '1\n'",
    "nw worker poll j/1", "nw worker poll j-1 --renew --from 2",
    "nw worker cancel", "nw worker cancel j-1 j-2", "nw worker cancel '$(id)'",
    "nw worker jobs --all",
    "nw worker poll --renew", "nw worker attach --help", "nw worker attach -x --from 0",
    "nw worker cancel --help",
])
def test_ssh_command_refuses_bad_job_or_cursor(execs, cmd):
    assert W.ssh_command({"SSH_ORIGINAL_COMMAND": cmd}) == 2
    assert execs == []


def test_info_reports_the_async_feature(wenv):
    r = LB.run(wenv, "nw worker info --json")
    rec = json.loads(b"\n".join(r.lines))
    assert rec["protocol"] == "nw.job.v1" and rec["features"] == ["async"]


def test_the_worker_cli_has_the_async_commands():
    from click.testing import CliRunner

    from nightwright.agentic import cli_worker
    assert {"submit", "attach", "poll", "cancel", "jobs"} <= set(cli_worker.worker_group.commands)
    r = CliRunner().invoke(cli_worker.worker_group, ["attach", "--help"])
    assert r.exit_code == 0 and "--from" in r.output
    r = CliRunner().invoke(cli_worker.worker_group, ["attach", "j-1", "--from", "-1"])
    assert r.exit_code == 2                                 # --from is a non-negative integer


# ---- review round 2: accepted -> lost, the kill path's guards, edge commands ------------------

def _async_dir(env, job_id, lock=True, **job):
    """A submit job dir with no job process (yet): `header.json`, `job.json`, `job.lock`."""
    d = W.jobs_dir(_cfg(env)) / job_id
    d.mkdir(parents=True)
    (d / "header.json").write_text("{}\n")
    (d / "job.json").write_text(json.dumps({"job_id": job_id, "state": "accepted",
                                            "header_digest": "x", **job}) + "\n")
    if lock:
        (d / "job.lock").touch()
    return d


def _flock(path):
    import fcntl
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def test_an_accepted_job_that_never_claims_proc_json_goes_lost(wenv):
    env = _conf(wenv, job_lost_after_s=30)
    old = time.time() - 100
    _async_dir(env, "j-spawned-old", spawned_ts=old, submitted_ts=old)
    _async_dir(env, "j-spawned-new", spawned_ts=time.time(), submitted_ts=old)
    _async_dir(env, "j-sub-old", submitted_ts=old)          # no spawned_ts, lock free
    held = _async_dir(env, "j-sub-held", submitted_ts=old)  # submit still spooling
    fd = _flock(held / "job.lock")
    try:
        assert _poll(env, "j-spawned-old")[-1]["state"] == "lost"
        assert _poll(env, "j-sub-old")[-1]["state"] == "lost"   # timed from submitted_ts
        assert _poll(env, "j-spawned-new")[-1]["state"] == "accepted"
        assert _poll(env, "j-sub-held")[-1]["state"] == "accepted"
        states = {k: v["state"] for k, v in _nw_jobs(env).items()}
        assert states == {"j-spawned-old": "lost", "j-sub-old": "lost",
                          "j-spawned-new": "accepted", "j-sub-held": "accepted"}
        # job.json mirrors the derived state
        assert _read(_job_dir(env, "j-spawned-old") / "job.json")["state"] == "lost"
        assert _read(_job_dir(env, "j-spawned-new") / "job.json")["state"] == "accepted"
    finally:
        os.close(fd)


def test_a_reader_never_creates_job_lock(wenv):
    d = _async_dir(wenv, "j-claiming", lock=False, submitted_ts=time.time() - 3600)
    assert _poll(wenv, "j-claiming")[-1]["state"] == "accepted"     # a claim in progress
    assert _nw_jobs(wenv)["j-claiming"]["state"] == "accepted"
    assert not (d / "job.lock").exists()


def test_reap_never_signals_a_recycled_pid(wenv, monkeypatch):
    import subprocess

    from nightwright.agentic import runproc
    monkeypatch.setenv(WJ.KILL_GRACE_ENV, "0.2")
    sleeper = subprocess.Popen(["sleep", "600"], start_new_session=True)
    try:
        ticks = runproc.start_ticks(sleeper.pid)
        d = _async_dir(wenv, "j-recycled")
        me = {"pid": sleeper.pid, "pgid": sleeper.pid}
        (d / "proc.json").write_text(json.dumps(
            {**me, "start_ticks": ticks + 1, "started_ts": time.time(),
             "gates": [{**me, "start_ticks": ticks + 7}, {**me, "start_ticks": None}]}))
        (d / "spawn.json").write_text(json.dumps({**me, "start_ticks": ticks + 3}))
        out = io.BytesIO()
        assert WJ.cancel(_cfg(wenv), "j-recycled", out) == 0
        assert json.loads(out.getvalue().splitlines()[-1])["state"] == "lost"
        assert _read(d / "result.json")["state"] == "lost"
        assert sleeper.poll() is None                       # never signalled
    finally:
        sleeper.kill()
        sleeper.wait(timeout=WAIT_S)


def test_reap_kills_a_slow_starting_job_process_before_recording_lost(wenv, monkeypatch):
    import subprocess
    import sys

    from nightwright.agentic import runproc
    monkeypatch.setenv(WJ.KILL_GRACE_ENV, "0.5")
    env = _conf(wenv, job_lost_after_s=2)
    d = _async_dir(env, "j-slow", spawned_ts=time.time() - 10)
    # the "job process": it holds job.lock (as one handed it by submit does) but never got
    # as far as proc.json
    child = subprocess.Popen(
        [sys.executable, "-c", "import fcntl, os, sys, time\n"
         f"fd = os.open({str(d / 'job.lock')!r}, os.O_RDWR)\n"
         "fcntl.flock(fd, fcntl.LOCK_EX); print('locked', flush=True); time.sleep(600)"],
        stdout=subprocess.PIPE, start_new_session=True)
    try:
        assert child.stdout.readline() == b"locked\n"
        # unknown to the kill path (no spawn.json): the lock stays held, nothing is recorded
        out = io.BytesIO()
        assert WJ.cancel(_cfg(env, job_lost_after_s=2), "j-slow", out) == 1
        last = json.loads(out.getvalue().splitlines()[-1])
        assert last["state"] == "lost" and last["reaped"] is False
        assert not (d / "result.json").exists() and child.poll() is None
        (d / "spawn.json").write_text(json.dumps(
            {"pid": child.pid, "pgid": child.pid,
             "start_ticks": runproc.start_ticks(child.pid)}))
        assert WJ.cancel(_cfg(env, job_lost_after_s=2), "j-slow", out) == 0
        assert child.wait(timeout=WAIT_S) is not None       # killed first ...
        assert _read(d / "result.json")["state"] == "lost"  # ... then recorded lost
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=WAIT_S)


def test_unknown_job_and_cancel_on_a_running_gate_job_exit_1(src, tmp_path, wenv):
    for cmd in ("poll nope", "attach nope --from 0", "cancel nope"):
        r = LB.run(wenv, f"nw worker {cmd}")
        assert r.returncode == 1 and _recs(r)[-1]["state"] == "unknown", cmd
    pidfile = tmp_path / "sleep.pid"
    h, data = _bundle_job(src, tmp_path, [_sleeper(pidfile)])
    job = Job(wenv, h, data)
    pid = _sleep_pid(pidfile)
    r = LB.run(wenv, f"nw worker cancel {h['job_id']}")
    assert r.returncode == 1 and _recs(r)[-1]["state"] == "running"
    assert _alive(pid) and not (_job_dir(wenv, h["job_id"]) / "cancel").exists()
    job.close()                                             # its stream owns it
    fin = job.final()
    assert fin["type"] == "done" and fin["interrupted"] is True
    assert until(lambda: not _alive(pid))


def test_a_broken_off_upload_leaves_the_job_id_free(src, tmp_path, wenv):
    h, data = _bundle_job(src, tmp_path, [_gate("x", "true")])
    cut = _submit(wenv, h, data[: len(data) // 2])
    assert cut.returncode == 3
    (err,) = _recs(cut)
    assert err["type"] == "error" and err["stage"] == "read" and "n" not in err
    assert not _job_dir(wenv, h["job_id"]).exists()
    # a submit that died mid-upload (no cleanup): the never-spawned, unlocked dir is reclaimed
    d = _async_dir(wenv, h["job_id"], header_digest=WJ.header_digest(h),
                   submitted_ts=time.time())
    (d / "bundle").write_bytes(data[:10])
    again = _submit(wenv, h, data)
    assert again.returncode == 0, again.stderr
    assert _recs(again)[0]["state"] == "accepted"
    a = _attach(wenv, h["job_id"])
    assert a.returncode == 0 and _recs(a)[-1]["type"] == "done"


def test_submit_refuses_a_job_id_that_reads_as_an_option(src):
    out = io.BytesIO()
    h = _header("-x", src.head, src.base, [_gate("x", "true")], bundle_bytes=0)
    rc = WJ.submit({"data_dir": "/nonexistent"}, io.BytesIO((json.dumps(h) + "\n").encode()), out)
    (err,) = [json.loads(ln) for ln in out.getvalue().splitlines()]
    assert rc == 3 and err["stage"] == "validate"


# ---- review round 3 --------------------------------------------------------------------------

def test_submit_writes_the_lease_before_it_spawns_the_job_process(src, tmp_path, wenv,
                                                                  monkeypatch):
    seen, real = [], WJ._spawn

    def spawn(cfg, job, lock_fd):
        seen.append(_read(job / "lease.json"))
        seen.append(time.time())
        return real(cfg, job, lock_fd)
    monkeypatch.setattr(WJ, "_spawn", spawn)
    h, data = _bundle_job(src, tmp_path, [_gate("x", "true")])
    out = io.BytesIO()
    assert WJ.submit(_cfg(wenv, job_lease_s=120), io.BytesIO((json.dumps(h) + "\n").encode()
                                                              + data), out) == 0
    lease, at_spawn = seen
    assert lease is not None                                # written before the spawn
    assert at_spawn - 5 <= lease["lease_until"] - 120 <= at_spawn
    assert _attach(wenv, h["job_id"]).returncode == 0


def test_poll_renew_keeps_the_job_alive_and_plain_poll_does_not_renew(src, tmp_path, wenv):
    env = _conf(wenv, job_lease_s=3)
    pidfile = tmp_path / "sleep.pid"
    h, data = _bundle_job(src, tmp_path, [_sleeper(pidfile)])
    job = _job_dir(env, h["job_id"])
    # submitted with a long first lease, so the 3 s one starts with the first renewal
    assert WJ.submit(_cfg(env, job_lease_s=600),
                     io.BytesIO((json.dumps(h) + "\n").encode() + data), io.BytesIO()) == 0
    pid = _sleep_pid(pidfile)
    t0 = time.time()
    assert _poll(env, h["job_id"], renew=True)[-1]["state"] == "running"
    t1 = time.time()
    until_ = _read(job / "lease.json")["lease_until"]
    assert t0 + 3 <= until_ <= t1 + 3                       # lease_until = now + job_lease_s
    # renewing (in-process: no interpreter startup per call) for longer than two leases
    cfg = _cfg(env, job_lease_s=3)
    end = time.monotonic() + 7
    while time.monotonic() < end:
        before = time.time()
        WJ.poll(cfg, h["job_id"], 0, True, io.BytesIO())
        assert before + 3 <= _read(job / "lease.json")["lease_until"] <= time.time() + 3
        assert not (job / "result.json").exists() and _alive(pid)
        time.sleep(0.5)
    # a plain poll leaves lease.json as it is
    lease = (job / "lease.json").read_bytes()
    assert _poll(env, h["job_id"])[-1]["state"] == "running"
    WJ.poll(cfg, h["job_id"], 0, False, io.BytesIO())
    assert (job / "lease.json").read_bytes() == lease
    # no renewals: the job cancels itself
    res = _result(env, h["job_id"])
    assert res["state"] == "done" and res["frame"]["interrupted"] is True
    assert until(lambda: not _alive(pid))


@pytest.mark.parametrize("sig", ["SIGHUP", "SIGINT", "SIGTERM"])
def test_a_signal_or_eof_on_attach_never_cancels_the_job(wenv, sig):
    job = _fake_job(wenv, "j-sig")
    lease = job / "lease.json"
    p = LB.start(wenv, "nw worker attach j-sig --from 0", b"")     # stdin at EOF from the start
    assert until(lambda: lease.exists())                    # it is following (it renewed)
    p.send_signal(getattr(signal, sig))
    p.wait(timeout=WAIT_S)
    assert not (job / "cancel").exists()
    assert _state_in_process(wenv, "j-sig")["state"] == "running"
    assert _read(job / "job.json")["state"] == "running"


def test_heartbeat_age_is_monotonic_across_a_wall_clock_jump(wenv):
    """A host suspend or an NTP step moves the wall clock, not `CLOCK_MONOTONIC`: a fresh
    heartbeat whose wall `ts` reads 1000 s old is still `running`."""
    job = _fake_job(wenv, "j-mono")
    (job / "heartbeat.json").write_text(json.dumps(
        {"ts": time.time() - 1000, "pid": os.getpid(), "mono": time.monotonic(),
         "boot": WJ._boot()}))
    assert WJ._boot()                                       # a Linux worker has a boot id
    st = _state_in_process(wenv, "j-mono", job_lost_after_s=60)
    assert st["state"] == "running" and st["heartbeat_age_s"] < 5
    # a stale monotonic heartbeat is lost whatever the wall clock says
    (job / "heartbeat.json").write_text(json.dumps(
        {"ts": time.time(), "pid": os.getpid(), "mono": time.monotonic() - 100,
         "boot": WJ._boot()}))
    assert _state_in_process(wenv, "j-mono", job_lost_after_s=60)["state"] == "lost"
    # a heartbeat from another boot falls back to the wall clock
    (job / "heartbeat.json").write_text(json.dumps(
        {"ts": time.time(), "pid": os.getpid(), "mono": 1.0, "boot": "another-boot"}))
    assert _state_in_process(wenv, "j-mono", job_lost_after_s=60)["state"] == "running"


def test_a_real_job_heartbeats_on_the_monotonic_clock(src, tmp_path, wenv):
    pidfile = tmp_path / "sleep.pid"
    h, data = _bundle_job(src, tmp_path, [_sleeper(pidfile)])
    assert _submit(wenv, h, data).returncode == 0
    _sleep_pid(pidfile)
    job = _job_dir(wenv, h["job_id"])
    hb = _read(job / "heartbeat.json")
    proc = _proc(wenv, h["job_id"])
    assert hb["boot"] == proc["boot"] == WJ._boot()
    assert isinstance(hb["mono"], float) and isinstance(proc["started_mono"], float)
    assert LB.run(wenv, f"nw worker cancel {h['job_id']}").returncode == 0
    assert _result(wenv, h["job_id"])["state"] == "done"


def test_cancel_on_a_dead_gate_job_dir_never_writes_into_it(wenv):
    d = W.jobs_dir(_cfg(wenv)) / "j-gdead"                  # SPEC-0336 dir, its process gone
    d.mkdir(parents=True)
    (d / "job.json").write_text(json.dumps({"job_id": "j-gdead", "state": "running"}) + "\n")
    (d / "job.lock").touch()
    assert _poll(wenv, "j-gdead")[-1]["state"] == "lost"
    r = LB.run(wenv, "nw worker cancel j-gdead")
    assert r.returncode == 1 and _recs(r)[-1]["state"] == "lost"
    assert not (d / "result.json").exists()
    assert _read(d / "job.json") == {"job_id": "j-gdead", "state": "running"}


def test_the_kill_path_runs_without_jobs_lock(wenv, monkeypatch):
    """A reap's kill graces never block a claim: while `reap` signals, `jobs_lock` is free."""
    import fcntl
    d = _async_dir(wenv, "j-free", spawned_ts=time.time() - 3600)
    lock_free = []

    def kill_group(child, grace, cont=False):
        fd = os.open(W.worker_dir(_cfg(wenv)) / "jobs.lock", os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_free.append(True)
        except OSError:
            lock_free.append(False)
        finally:
            os.close(fd)
    (d / "spawn.json").write_text(json.dumps({"pid": os.getpid(), "pgid": os.getpgid(0),
                                              "start_ticks": 1}))
    monkeypatch.setattr(WJ, "_pid_alive", lambda leader: True)
    monkeypatch.setattr(WJ, "_kill_group", kill_group)
    out = io.BytesIO()
    assert WJ.cancel(_cfg(wenv, job_lost_after_s=2), "j-free", out) == 0
    assert lock_free == [True]
    assert json.loads(out.getvalue().splitlines()[-1])["reaped"] is True
    assert _read(d / "result.json")["state"] == "lost"
    lock_free.clear()
    d2 = _async_dir(wenv, "j-free2", spawned_ts=time.time() - 3600)
    (d2 / "spawn.json").write_text((d / "spawn.json").read_text())
    rec = W.gc(_cfg(wenv, job_lost_after_s=2), keep_days=0)
    assert rec["jobs_reaped"] == ["j-free2"] and lock_free == [True]
    assert {"j-free", "j-free2"} <= set(rec["jobs_removed"])


def test_a_derived_lost_never_overwrites_the_jobs_own_final_state(wenv):
    job = _fake_job(wenv, "j-race")
    proc = _read(job / "proc.json")
    (job / "proc.json").write_text(json.dumps({**proc, "start_ticks": proc["start_ticks"] + 1}))
    # the job process wrote its final job.json; its result.json is about to follow
    (job / "job.json").write_text(json.dumps({"job_id": "j-race", "state": "done",
                                              "header_digest": "x"}) + "\n")
    WJ._mirror(_cfg(wenv), job, "lost", locked=False)
    assert _read(job / "job.json")["state"] == "done"
    # result.json, once there, is what a mirror writes
    (job / "job.json").write_text(json.dumps({"job_id": "j-race", "state": "running",
                                              "header_digest": "x"}) + "\n")
    (job / "result.json").write_text(json.dumps({"state": "done", "frame": None}))
    WJ._mirror(_cfg(wenv), job, "lost", locked=True)
    assert _read(job / "job.json")["state"] == "done"


def test_the_reader_skips_frames_below_the_cursor_without_parsing_them(tmp_path, monkeypatch):
    def frame(n, type_="gate", **kw):                       # as `_Frames.emit`: `n` last
        return (json.dumps({"schema": W.FRAME_SCHEMA, "type": type_, "job_id": "j", **kw,
                            "n": n}) + "\n").encode()
    path = tmp_path / "frames.jsonl"
    big = "x" * (3 * WJ._Reader.CHUNK)                      # spans several chunks
    path.write_bytes(b"".join(frame(n, blob=big) for n in range(1, 4))
                     + frame(4, "done", ok=True)[:-30])
    parsed, real = [], json.loads
    monkeypatch.setattr(WJ.json, "loads", lambda raw, *a, **k: (parsed.append(len(raw)),
                                                                real(raw, *a, **k))[1])
    r = WJ._Reader(path, 3)
    assert list(r.read()) == [] and parsed == []            # 1..3 skipped by their tail
    tail = len(frame(4, "done", ok=True)) - 30
    assert r.pos == path.stat().st_size - tail              # stopped at the last newline
    with open(path, "ab") as f:
        f.write(frame(4, "done", ok=True)[-30:])
    (line,) = list(r.read())
    assert r.pos == path.stat().st_size                     # the partial line was re-read whole
    assert real(line)["n"] == 4 and r.final == "done" and len(parsed) == 1
    r0 = WJ._Reader(path, 0)
    assert [real(ln)["n"] for ln in r0.read()] == [1, 2, 3, 4]


# ---- review round 4: drained resubmits, a dead leader's group, the job's own final state, ---
# ---- torn frame appends -----------------------------------------------------------------

def test_a_resubmit_takes_the_whole_upload_before_it_answers(src, wenv):
    big = 4 << 20                                           # far over a pipe's buffer
    h = _header("j-drain", src.head, src.base, [_gate("x", "true")], bundle_bytes=big)
    _async_dir(wenv, "j-drain", header_digest=WJ.header_digest(h), spawned_ts=time.time())
    for hdr, rc, type_ in ((h, 0, "status"), ({**h, "attempt": 1}, 3, "error")):
        p = LB.start(wenv, "nw worker submit")
        # header + bundle in full before stdout is read, as a plain client writes them
        p.stdin.write((json.dumps(hdr) + "\n").encode() + b"\0" * big)    # no EPIPE
        p.stdin.close()
        out = p.stdout.read()
        assert p.wait(timeout=WAIT_S) == rc, p.stderr.read()
        p.stdout.close()
        p.stderr.close()
        (rec,) = [json.loads(ln) for ln in out.splitlines()]
        assert rec["type"] == type_
    assert not (_job_dir(wenv, "j-drain") / "bundle").exists()     # nothing new was spooled


def _orphaned_group():
    """A process group whose leader has exited and whose child (`sleep`) lives on, as when
    the job process is OOM-killed during its checkout's `git fetch`."""
    import subprocess
    import sys

    from nightwright.agentic import runproc
    leader = subprocess.Popen(
        [sys.executable, "-c", "import subprocess, sys\n"
         "c = subprocess.Popen(['sleep', '600'])\n"
         "print(c.pid, flush=True); sys.stdin.read()"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, start_new_session=True)
    child = int(leader.stdout.readline())
    rec = {"pid": leader.pid, "pgid": leader.pid, "start_ticks": runproc.start_ticks(leader.pid),
           "started_ts": time.time(), "gates": []}
    leader.stdin.close()                                    # the leader exits ...
    leader.wait(timeout=WAIT_S)
    leader.stdout.close()
    assert _alive(child)                                    # ... and its child does not
    return rec, child


@pytest.mark.parametrize("own", ["running", "done"])
def test_reap_ends_a_dead_leaders_surviving_group(wenv, monkeypatch, own):
    monkeypatch.setenv(WJ.KILL_GRACE_ENV, "0.5")
    rec, child = _orphaned_group()
    try:
        d = _async_dir(wenv, f"j-orphan-{own}", state=own, spawned_ts=time.time())
        (d / "proc.json").write_text(json.dumps(rec))
        # the group still runs: the job is lost (gc would reap it), whatever job.json says
        assert _state_in_process(wenv, d.name)["state"] == "lost"
        out = io.BytesIO()
        assert WJ.cancel(_cfg(wenv), d.name, out) == 0
        assert json.loads(out.getvalue().splitlines()[-1])["reaped"] is True
        assert until(lambda: not _alive(child))
        # a job that wrote its own final job.json before it died keeps that state
        want = "done" if own == "done" else "lost"
        assert _read(d / "result.json")["state"] == want
        assert _read(d / "job.json")["state"] == want
        assert _state_in_process(wenv, d.name)["state"] == want
    finally:
        if _alive(child):
            os.kill(child, signal.SIGKILL)


def test_a_job_that_died_after_its_final_job_json_reads_done(wenv):
    import subprocess

    from nightwright.agentic import runproc
    gone = subprocess.Popen(["true"], start_new_session=True)
    ticks = runproc.start_ticks(gone.pid)
    gone.wait(timeout=WAIT_S)
    d = _async_dir(wenv, "j-cleanup-died", state="done", spawned_ts=time.time())
    (d / "proc.json").write_text(json.dumps({"pid": gone.pid, "pgid": gone.pid,
                                             "start_ticks": ticks or 1,
                                             "started_ts": time.time(), "gates": []}))
    # its pid is dead, nothing of its group is left and job.json says done: no result.json,
    # but every reader agrees with frames.jsonl's final `done`
    assert _poll(wenv, "j-cleanup-died")[-1]["state"] == "done"
    assert _nw_jobs(wenv)["j-cleanup-died"]["state"] == "done"
    r = LB.run(wenv, "nw worker cancel j-cleanup-died")
    assert r.returncode == 0 and _recs(r)[-1]["state"] == "done"
    assert _read(d / "job.json")["state"] == "done"
    assert "jobs_reaped" not in W.gc(_cfg(wenv), keep_days=30)


@pytest.mark.parametrize("cut_fails", [False, True])
def test_a_failed_frame_append_never_tears_the_next_frame(tmp_path, monkeypatch, cut_fails):
    import errno
    log = tmp_path / "frames.jsonl"
    frames = W._Frames("j", io.BytesIO())
    frames.log = log
    frames.emit("accepted")
    real_write, fail = os.write, []

    def short_then_enospc(fd, data):                        # the disk fills mid-frame
        if fail:
            fail.clear()
            real_write(fd, bytes(data[:len(data) // 2]))
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write(fd, data)

    monkeypatch.setattr(W.os, "write", short_then_enospc)
    if cut_fails:
        def no_truncate(fd, size):
            raise OSError(errno.EIO, "I/O error")
        monkeypatch.setattr(W.os, "ftruncate", no_truncate)
    fail.append(True)
    frames.emit("gate", blob="x" * 100_000)
    frames.emit("done", ok=True)
    monkeypatch.undo()
    got = [json.loads(ln) for ln in WJ._Reader(log, 0).read()]
    # the torn frame is gone (or a skipped line of its own); `done` arrives whole
    assert [f["type"] for f in got] == ["accepted", "done"]
    # a cut-off frame's `n` is reused (no gap); a torn one's never is (no repeat)
    assert [f["n"] for f in got] == ([1, 3] if cut_fails else [1, 2])
    assert WJ._last_n(log.parent) == got[-1]["n"]
    sent = [json.loads(ln) for ln in frames.stream.getvalue().splitlines()]
    assert [f["type"] for f in sent] == ["accepted", "gate", "done"]
    # stdout's copy of a frame not whole in the log is unnumbered
    assert "n" not in sent[1] and sent[2]["n"] == got[-1]["n"]


# ---- review round 5: a reader across a cut-back append, a vanished job dir ----------------

def _nframe(n, type_="gate", **kw):                          # as `_Frames.emit`: `n` last
    return (json.dumps({"schema": W.FRAME_SCHEMA, "type": type_, "job_id": "j", **kw,
                        "n": n}) + "\n").encode()


@pytest.mark.parametrize("longer", [False, True])
def test_a_reader_survives_a_cut_back_append(tmp_path, longer):
    path = tmp_path / "frames.jsonl"
    path.write_bytes(_nframe(1, "accepted"))
    r = WJ._Reader(path, 0)
    assert [json.loads(ln)["n"] for ln in r.read()] == [1]
    failed = _nframe(2, blob="x" * 5000)
    with open(path, "ab") as f:                             # half a frame, then the disk fills
        f.write(failed[:len(failed) // 2])
    assert list(r.read()) == []                             # the reader saw half of it
    os.truncate(path, len(_nframe(1, "accepted")))           # `_append` cuts it back off ...
    new = _nframe(2, "done", ok=True, **({"blob": "y" * 9000} if longer else {}))
    with open(path, "ab") as f:                             # ... and `n` 2 is written again
        f.write(new)
    got = [json.loads(ln) for ln in r.read()]
    assert [(f["type"], f["n"]) for f in got] == [("done", 2)] and r.final == "done"
    assert json.dumps(got[0]).encode() + b"\n" == new        # the new frame, nothing stale
    assert list(r.read()) == []                             # emitted once


def test_attach_follows_a_cut_back_append_to_the_rewritten_done(tmp_path, monkeypatch):
    import errno
    log = tmp_path / "frames.jsonl"
    frames = W._Frames("j", io.BytesIO())
    frames.log = log
    frames.emit("accepted")
    r = WJ._Reader(log, 0)
    assert [json.loads(ln)["type"] for ln in r.read()] == ["accepted"]
    real_write = os.write

    def short_then_enospc(fd, data):                        # half the gate frame lands ...
        real_write(fd, bytes(data[:len(data) // 2]))
        assert list(r.read()) == []                         # ... and the reader sees it
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(W.os, "write", short_then_enospc)
    frames.emit("gate", blob="x" * 100_000)
    monkeypatch.undo()
    frames.emit("done", ok=True)                            # same offset, same `n`
    got = [json.loads(ln) for ln in r.read()]
    assert [(f["type"], f["n"]) for f in got] == [("done", 2)] and r.final == "done"


def test_attach_and_poll_report_a_removed_job_dir(wenv, monkeypatch):
    _async_dir(wenv, "j-vanish", spawned_ts=time.time())
    real_state = WJ.job_state

    def state_then_gone(cfg, job, *a, **k):                 # gc removes it mid-follow
        st = real_state(cfg, job, *a, **k)
        shutil.rmtree(job, ignore_errors=True)
        return st

    monkeypatch.setattr(WJ, "job_state", state_then_gone)
    out = io.BytesIO()
    assert WJ.attach(_cfg(wenv), "j-vanish", 0, out) == WJ.EXIT_UNKNOWN
    rec = json.loads(out.getvalue().splitlines()[-1])
    assert rec["type"] == "status" and rec["state"] == "unknown" and rec["n"] == 0
    monkeypatch.undo()
    _async_dir(wenv, "j-vanish2", spawned_ts=time.time())
    real_renew = WJ._renew

    def gone_then_renew(job, lease_s):                      # removed just before a renewal
        shutil.rmtree(job, ignore_errors=True)
        real_renew(job, lease_s)

    monkeypatch.setattr(WJ, "_renew", gone_then_renew)
    out = io.BytesIO()
    assert WJ.attach(_cfg(wenv), "j-vanish2", 0, out) == WJ.EXIT_UNKNOWN
    assert json.loads(out.getvalue().splitlines()[-1])["state"] == "unknown"
    _async_dir(wenv, "j-vanish3", spawned_ts=time.time())
    out = io.BytesIO()
    assert WJ.poll(_cfg(wenv), "j-vanish3", 0, True, out) == WJ.EXIT_UNKNOWN
    assert json.loads(out.getvalue().splitlines()[-1])["state"] == "unknown"
