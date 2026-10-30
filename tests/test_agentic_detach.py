"""SPEC-0291: `wt queue run --detach` (start a session-leader runner, confirm its lease) and
`wt queue stop` (SIGTERM the lease holder, honoring the close guard, never SIGKILL)."""
import datetime as dt
import json
import os
import signal
import subprocess
import sys
import threading

import pytest

from test_agentic_e2e import impl_done, make_cfg, make_repo, script
from waits import until
from wt.agentic import lease as L
from wt.agentic import queue as Q
from wt.agentic import store as S

# A child that takes (or not) the lease of the data dir in argv[1]; argv[2] is the mode.
CHILD = r"""
import signal, sys, time
from wt.agentic import lease as L
cfg = {"data_dir": sys.argv[1], "config_dir": sys.argv[1], "agentic": {"lease_ttl_s": 120}}
mode = sys.argv[2]
if mode == "boom":
    print("boom", flush=True)
    sys.exit(3)
if mode == "idle":
    time.sleep(600)
lease = L.acquire(cfg, ["child"])
if mode == "term":
    def _bye(*_):
        lease.release()
        sys.exit(0)
    signal.signal(signal.SIGTERM, _bye)
elif mode == "ignore":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
print("holding", flush=True)
while True:
    time.sleep(0.1)
"""


def _cfg(tmp_path):
    (tmp_path / "data").mkdir(exist_ok=True)
    return {"data_dir": str(tmp_path / "data"), "config_dir": str(tmp_path / "data"),
            "agentic": {"lease_ttl_s": 120, "profiles": {}}}


@pytest.fixture
def children():
    procs = []
    yield procs
    for p in procs:
        if p.poll() is None:
            p.kill()
        p.wait(10)


def _child_argv(cfg, mode):
    return [sys.executable, "-c", CHILD, cfg["data_dir"], mode]


def _spawn(cfg, mode, children):
    """A lease-holding child started directly (stop tests)."""
    p = subprocess.Popen(_child_argv(cfg, mode), stdout=subprocess.PIPE, text=True)
    children.append(p)
    assert p.stdout.readline().strip() == "holding"
    return p


def _recording_popen(children):
    def popen(argv, **kw):
        p = subprocess.Popen(argv, **kw)
        children.append(p)
        return p
    return popen


# ---- detach -------------------------------------------------------------------------------------

def test_detach_refuses_when_held(tmp_path):
    cfg = _cfg(tmp_path)
    with L.acquire(cfg):
        with pytest.raises(ValueError, match=rf"queue held by pid {os.getpid()} .*wt queue stop"):
            Q.detach_runner(cfg, ["wt", "queue", "run", "--detach"], tmp_path / "r.log",
                            popen=lambda *a, **k: pytest.fail("must not start a child"))


def test_detach_waits_for_child_lease(tmp_path, children):
    cfg = _cfg(tmp_path)
    log = tmp_path / "logs" / "runner.log"
    d = Q.detach_runner(cfg, _child_argv(cfg, "hold"), log, popen=_recording_popen(children))
    (child,) = children
    assert d == {"pid": child.pid, "log": str(log)}
    assert L.lease_status(cfg)["pid"] == child.pid
    assert until(lambda: "holding" in log.read_text())
    assert log.read_text().startswith("--- wt queue run --detach ")


def test_detach_child_dies_early(tmp_path, children):
    cfg = _cfg(tmp_path)
    with pytest.raises(ValueError, match=r"exited with status 3") as e:
        Q.detach_runner(cfg, _child_argv(cfg, "boom"), tmp_path / "r.log",
                        popen=_recording_popen(children))
    assert "boom" in str(e.value)


def test_detach_timeout_leaves_child(tmp_path, children):
    cfg = _cfg(tmp_path)
    with pytest.raises(ValueError, match=r"did not take the lease within 0.5s") as e:
        Q.detach_runner(cfg, _child_argv(cfg, "idle"), tmp_path / "r.log", timeout_s=0.5,
                        popen=_recording_popen(children))
    (child,) = children
    assert f"pid {child.pid}" in str(e.value) and "not killed" in str(e.value)
    assert child.poll() is None                     # left running; the fixture kills it


def test_detach_popen_args(tmp_path):
    cfg = _cfg(tmp_path)
    calls = []

    class _Child:
        pid = 4242

        def poll(self):
            return 7

    def popen(argv, **kw):
        calls.append((argv, kw))
        return _Child()

    log = tmp_path / "r.log"
    with pytest.raises(ValueError):
        Q.detach_runner(cfg, ["/bin/wt", "queue", "run", "--detach", "--log", str(log),
                              "--poll-interval", "5", "--log=x", "--no-reexec"], log, popen=popen)
    ((argv, kw),) = calls
    assert argv == ["/bin/wt", "queue", "run", "--poll-interval", "5", "--no-reexec"]
    assert kw["start_new_session"] is True and kw["stdin"] is subprocess.DEVNULL
    assert kw["stderr"] is subprocess.STDOUT and kw["stdout"].name == str(log)
    assert kw["env"]["WT_RUNNER_LOG"] == str(log)


def test_run_records_log_in_lease(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    monkeypatch.setenv("WT_RUNNER_LOG", "/x/runner.log")
    seen = []
    Q.run(cfg, once=True, on_tick=lambda _r: seen.append(L.lease_status(cfg)))
    assert seen and seen[0]["log"] == "/x/runner.log" and seen[0]["detached"] is True
    assert L.read_lease(cfg) is None                # released after the tick


def test_reexec_adoption_keeps_log_and_detached(tmp_path, monkeypatch):
    """A re-exec (SPEC-0194) adopts the lease file, so `log` / `detached` survive it."""
    cfg = _cfg(tmp_path)
    lease = L.acquire(cfg)
    lease.update_info(log="/x/runner.log", detached=True)
    monkeypatch.setenv(L.HANDOVER_ENV, lease.handover()[L.HANDOVER_ENV])
    adopted = L.acquire(cfg)
    try:
        st = L.lease_status(cfg)
        assert st["held"] and st["log"] == "/x/runner.log" and st["detached"] is True
        assert "restarted_at" in adopted.info
    finally:
        adopted.release()


def test_lease_status_without_detach_has_null_detached(tmp_path):
    cfg = _cfg(tmp_path)
    with L.acquire(cfg):
        st = L.lease_status(cfg)
    assert st["log"] is None and st["detached"] is None


# ---- close guard --------------------------------------------------------------------------------

NOW = dt.datetime(2026, 10, 30, 12, 0, tzinfo=dt.timezone.utc)


def _status(kind, age_s, budget_s=600, since=None):
    since = since if since is not None else (NOW - dt.timedelta(seconds=age_s)).isoformat()
    return {"held": True, "busy": {"kind": kind, "campaign": "C-1", "since": since,
                                   "budget_s": budget_s}}


def test_close_guard_open_within_budget():
    st = _status("open", 100)
    assert Q.close_guard(st, now=NOW) == st["busy"]
    assert Q.close_guard(_status("close", 600), now=NOW) is not None     # at the budget


def test_close_guard_expired():
    assert Q.close_guard(_status("close", 601), now=NOW) is None
    assert Q.close_guard(_status("open", 5000), now=NOW) is None


def test_close_guard_drain_not_guarded():
    assert Q.close_guard(_status("drain", 0), now=NOW) is None
    assert Q.close_guard({"held": True, "busy": None}, now=NOW) is None
    assert Q.close_guard({"held": False}, now=NOW) is None


def test_close_guard_bad_since_guarded():
    assert Q.close_guard(_status("close", 0, since="not a time"), now=NOW) is not None
    assert Q.close_guard(_status("open", 0, since=""), now=NOW) is not None


# ---- stop ---------------------------------------------------------------------------------------

def _set_busy(cfg, busy):
    info = L.read_lease(cfg)
    if busy is None:
        info.pop("busy", None)
    else:
        info["busy"] = busy
    L.lease_path(cfg).write_text(json.dumps(info))


def test_stop_no_runner_ok(tmp_path):
    cfg = _cfg(tmp_path)
    assert Q.stop_runner(cfg, kill=lambda *a: pytest.fail("no kill")) == {
        "stopped": False, "reason": "no runner"}


def test_stop_sigterm_releases(tmp_path, children):
    cfg = _cfg(tmp_path)
    child = _spawn(cfg, "term", children)
    sent = []

    def kill(pid, sig):
        sent.append((pid, sig))
        os.kill(pid, sig)

    r = Q.stop_runner(cfg, timeout_s=30, kill=kill)
    assert r["stopped"] is True and r["pid"] == child.pid and r["waited_s"] >= 0
    assert sent == [(child.pid, signal.SIGTERM)]
    assert not L.is_held(cfg)


def test_stop_timeout_no_sigkill(tmp_path, children):
    cfg = _cfg(tmp_path)
    child = _spawn(cfg, "ignore", children)
    sent = []

    def kill(pid, sig):
        sent.append(sig)
        os.kill(pid, sig)

    with pytest.raises(ValueError, match=rf"pid {child.pid} did not release the lease within "
                                         r"0.5s \(still running\); not killed"):
        Q.stop_runner(cfg, timeout_s=0.5, kill=kill)
    assert sent == [signal.SIGTERM]
    assert child.poll() is None and L.is_held(cfg)


def test_stop_refuses_in_guard(tmp_path, children):
    cfg = _cfg(tmp_path)
    _spawn(cfg, "term", children)
    _set_busy(cfg, {"kind": "close", "campaign": "C-9", "since": S.now_iso(), "budget_s": 600})
    with pytest.raises(ValueError, match=r"close guard: close of C-9 .*budget 600s; retry "
                                         r"later or pass --wait-guard"):
        Q.stop_runner(cfg, kill=lambda *a: pytest.fail("no kill inside the guard"))
    assert L.is_held(cfg)


def test_stop_wait_guard_then_stops(tmp_path, children):
    cfg = _cfg(tmp_path)
    child = _spawn(cfg, "term", children)
    _set_busy(cfg, {"kind": "open", "campaign": "C-9", "since": S.now_iso(), "budget_s": 600})
    t = threading.Timer(0.3, _set_busy, (cfg, None))
    t.start()
    try:
        r = Q.stop_runner(cfg, timeout_s=30, wait_guard=True)
    finally:
        t.join()
    assert r["stopped"] is True and r["pid"] == child.pid


def test_stop_wait_guard_times_out(tmp_path, children):
    cfg = _cfg(tmp_path)
    _spawn(cfg, "term", children)
    _set_busy(cfg, {"kind": "open", "campaign": "C-9", "since": S.now_iso(), "budget_s": 600})
    with pytest.raises(ValueError, match=r"still guarded after 0.3s"):
        Q.stop_runner(cfg, wait_guard=True, guard_wait_s=0.3,
                      kill=lambda *a: pytest.fail("no kill inside the guard"))


def test_stop_other_host_refused(tmp_path, children):
    cfg = _cfg(tmp_path)
    _spawn(cfg, "term", children)
    info = L.read_lease(cfg)
    info["host"] = "elsewhere.example"
    L.lease_path(cfg).write_text(json.dumps(info))
    with pytest.raises(ValueError, match=r"on elsewhere.example, not this host"):
        Q.stop_runner(cfg, kill=lambda *a: pytest.fail("no remote kill"))
