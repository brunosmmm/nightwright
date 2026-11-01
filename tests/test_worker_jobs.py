"""SPEC-0340: worker-owned async jobs (`nw worker submit|attach|poll|cancel|jobs`), offline. Every
call goes through `tests/remote_loopback.py` (the forced command, run locally in place of
`ssh`), except where a timing bound is checked in-process."""
import hashlib
import io
import json
import os
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


def test_reattach_after_a_dropped_connection_loses_and_repeats_nothing(src, tmp_path, wenv):
    marker = tmp_path / "go"
    h, data = _bundle_job(src, tmp_path, [_gate("one", "echo one"),
                                          _gate("two", _wait_file(marker))])
    assert _submit(wenv, h, data).returncode == 0
    first = _attach(wenv, h["job_id"], kill_after=3)      # accepted, started, gate one
    assert first.dropped
    seen = _recs(first)
    assert [f["n"] for f in seen] == [1, 2, 3]
    # an attach killed by SIGTERM never cancels the job
    p = LB.start(wenv, f"nw worker attach {h['job_id']} --from 3", b"")
    time.sleep(0.3)
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
    (job / "job.json").write_text(json.dumps({"job_id": job_id, "state": "running"}) + "\n")
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
    time.sleep(0.8)                                       # over one follow tick
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
    assert _submit(env, h, data).returncode == 0
    pid = _sleep_pid(pidfile)
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

    assert _submit(env, h, data).returncode == 0
    w = threading.Thread(target=watch, daemon=True)
    w.start()
    try:
        a = _attach(env, h["job_id"])
    finally:
        stop.set()
        w.join(WAIT_S)
    assert a.returncode == 0, a.stderr
    frames = _recs(a)
    assert frames[-1]["type"] == "done"
    assert frames[-1]["ok"] is True and frames[-1]["interrupted"] is False
    assert not lapsed
    gaps = [b - a for a, b in zip(changes, changes[1:])]
    # min(30, 2/3) s apart, give or take a follow tick (plus one for a loaded machine);
    # `not lapsed` above is the hard bound
    assert gaps and max(gaps) <= 2 / 3 + 2 * WJ.FOLLOW_TICK_S, gaps


def test_attach_rewrites_the_lease_at_most_every_third_of_it():
    assert min(WJ.LEASE_REWRITE_MAX_S, WJ.DEFAULTS["job_lease_s"] / 3) == 30.0
    s = WJ.settings({"agentic": {"workers": {"job_lease_s": 2, "job_heartbeat_s": 0.5,
                                             "job_lost_after_s": "bad"}}})
    assert s == {"job_lease_s": 2.0, "job_heartbeat_s": 0.5, "job_lost_after_s": 60.0}
    assert WJ.settings({}) == {"job_lease_s": 900.0, "job_heartbeat_s": 10.0,
                               "job_lost_after_s": 60.0}


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
    assert until(lambda: _state_in_process(env, name, **LOST)["state"] == "lost", timeout=3.5)
    assert time.monotonic() - t0 <= 3.5
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
    env = _conf(wenv, job_lease_s=60)
    pidfile = tmp_path / "sleep.pid"
    h, data = _bundle_job(src, tmp_path, [_sleeper(pidfile)])
    assert _submit(env, h, data).returncode == 0
    sleep_pid = _sleep_pid(pidfile)
    p = LB.start(env, f"nw worker attach {h['job_id']} --from 0", b"")
    lines = []
    t = threading.Thread(target=lambda: lines.extend(p.stdout), daemon=True)
    t.start()
    assert until(lambda: any(json.loads(ln)["type"] == "started" for ln in lines))
    lease = _read(_job_dir(env, h["job_id"]) / "lease.json")
    os.kill(_proc(env, h["job_id"])["pid"], signal.SIGKILL)
    t0 = time.monotonic()
    assert p.wait(timeout=WAIT_S) == 4
    assert time.monotonic() - t0 <= 2.0
    t.join(WAIT_S)
    last = json.loads(lines[-1])
    assert last["type"] == "status" and last["state"] == "lost" and "heartbeat_age_s" in last
    time.sleep(0.6)
    assert _read(_job_dir(env, h["job_id"]) / "lease.json")["lease_until"] \
        <= lease["lease_until"] + 60                    # no renewal after the exit
    assert _poll(env, h["job_id"])[-1]["state"] == "lost"
    # the orphaned gate is reaped by cancel's kill path
    assert LB.run(env, f"nw worker cancel {h['job_id']}").returncode == 0
    assert until(lambda: not _alive(sleep_pid))


# ---- cancel ----------------------------------------------------------------------------------

def test_cancel_a_running_job_then_cancel_a_done_one(src, tmp_path, wenv):
    pidfile = tmp_path / "sleep.pid"
    h, data = _bundle_job(src, tmp_path, [_sleeper(pidfile)])
    assert _submit(wenv, h, data).returncode == 0
    pid = _sleep_pid(pidfile)
    r = LB.run(wenv, f"nw worker cancel {h['job_id']}")
    assert r.returncode == 0 and _recs(r)[-1]["cancel"] == "requested"
    res = _result(wenv, h["job_id"])
    assert res["state"] == "done" and res["frame"]["interrupted"] is True
    assert until(lambda: not _alive(pid))
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


@pytest.mark.parametrize("how", ["eof", "sighup"])
def test_gate_job_eof_and_sighup_still_cancel(src, tmp_path, wenv, how):
    pidfile = tmp_path / "sleep.pid"
    h, data = _bundle_job(src, tmp_path, [_sleeper(pidfile)])
    job = Job(wenv, h, data)
    pid = _sleep_pid(pidfile)
    if how == "eof":
        job.close()
    else:
        job.proc.send_signal(signal.SIGHUP)
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
    (acc / "job.json").write_text(json.dumps({"spawned_ts": time.time() + 600}) + "\n")
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
