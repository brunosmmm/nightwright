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
    log.parent.mkdir()
    log.write_text("previous runner's crash\n")
    d = Q.detach_runner(cfg, _child_argv(cfg, "hold"), log, popen=_recording_popen(children))
    (child,) = children
    assert d == {"pid": child.pid, "log": str(log)}
    assert L.lease_status(cfg)["pid"] == child.pid
    assert until(lambda: "holding" in log.read_text())
    assert log.read_text().startswith("previous runner's crash\n--- wt queue run --detach ")


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
    assert kw["stdout"].mode == "a"                 # appended, never truncated
    assert kw["env"]["WT_RUNNER_LOG"] == str(log)


def test_run_records_log_in_lease(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    monkeypatch.setenv("WT_RUNNER_LOG", "/x/runner.log")
    seen = []
    Q.run(cfg, once=True, on_tick=lambda _r: seen.append(L.lease_status(cfg)))
    assert seen and seen[0]["log"] == "/x/runner.log" and seen[0]["detached"] is True
    assert L.read_lease(cfg) is None                # released after the tick
    assert "WT_RUNNER_LOG" not in os.environ        # consumed: no persona run or gate inherits it


def test_nested_run_under_a_detached_runner_is_not_detached(tmp_path, monkeypatch):
    """A `Q.run` started by a descendant of a detached runner does not claim its log."""
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done(), impl_done()]))
    monkeypatch.setenv("WT_RUNNER_LOG", "/x/runner.log")
    Q.run(cfg, once=True)
    seen = []
    Q.run(cfg, once=True, on_tick=lambda _r: seen.append(L.lease_status(cfg)))
    assert seen and seen[0]["log"] is None and seen[0]["detached"] is None


def test_loop_reexec_passes_the_runner_log_on(tmp_path, monkeypatch):
    """The re-executed image records `log` / `detached` again even without the adopted file."""
    from test_agentic_reexec import _loop_with_commits
    monkeypatch.setenv("WT_RUNNER_LOG", "/x/runner.log")
    _, _, calls = _loop_with_commits(tmp_path, monkeypatch, [("src/wt/x.py", "A = 3\n")])
    ((_exe, _argv, env),) = calls
    assert env["WT_RUNNER_LOG"] == "/x/runner.log" and L.HANDOVER_ENV in env
    assert "WT_RUNNER_LOG" not in os.environ


def test_loop_reexec_without_detach_sets_no_runner_log(tmp_path, monkeypatch):
    from test_agentic_reexec import _loop_with_commits
    _, _, calls = _loop_with_commits(tmp_path, monkeypatch, [("src/wt/x.py", "A = 3\n")])
    ((_exe, _argv, env),) = calls
    assert "WT_RUNNER_LOG" not in env


def test_detach_log_oserror_is_a_clean_error(tmp_path):
    cfg = _cfg(tmp_path)
    blocker = tmp_path / "file"
    blocker.write_text("")
    with pytest.raises(ValueError, match=r"cannot start the runner"):
        Q.detach_runner(cfg, ["wt", "queue", "run"], blocker / "r.log",
                        popen=lambda *a, **k: pytest.fail("must not start a child"))


def test_detach_popen_oserror_is_a_clean_error(tmp_path):
    cfg = _cfg(tmp_path)
    with pytest.raises(ValueError, match=r"cannot start the runner \(/nonexistent/wt"):
        Q.detach_runner(cfg, ["/nonexistent/wt", "queue", "run"], tmp_path / "r.log")


def test_detach_poll_reads_the_lease_file_before_probing_the_flock(tmp_path, monkeypatch):
    """The parent's flock probe must not race the child's non-blocking acquire: no probe
    while the lease file does not name the child yet."""
    cfg = _cfg(tmp_path)
    probes = []
    real = L.is_held
    monkeypatch.setattr(L, "is_held", lambda c: probes.append(L.read_lease(c)) or real(c))

    held = []

    class _Child:
        pid = 4242

        def poll(self):
            if len(held) == 0 and len(probes) >= 1:
                held.append(L.acquire(cfg, ["child"]))   # "the child" takes the lease, and
                held[0].info["pid"] = self.pid           # the file names its pid
                held[0].update_info()
            return None

    try:
        d = Q.detach_runner(cfg, ["wt"], tmp_path / "r.log", popen=lambda *a, **k: _Child())
    finally:
        for lease in held:
            lease.release()
    assert d["pid"] == 4242
    # one probe before the start (nothing held), then only once the file names the child
    assert probes[0] is None and all(p and p["pid"] == 4242 for p in probes[1:])


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


# ---- how the runner re-runs itself (SPEC-0194 re-exec, SPEC-0291 detach) ------------------------

def _module_start(monkeypatch, *args):
    """Pretend this process was started as `python -m wt <args>`."""
    import types
    import wt
    main_py = os.path.join(os.path.dirname(wt.__file__), "__main__.py")
    monkeypatch.setitem(sys.modules, "__main__",
                        types.SimpleNamespace(__spec__=types.SimpleNamespace(name="wt.__main__")))
    monkeypatch.setattr(sys, "argv", [main_py, *args])
    return main_py


def test_module_start_reexec_and_detach_argv_are_runnable(monkeypatch):
    main_py = _module_start(monkeypatch, "queue", "run", "--help")
    argv = Q.self_argv()
    assert argv == [sys.executable, "-m", "wt", "queue", "run", "--help"]
    assert Q.entry_argv() == argv
    r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "--detach" in r.stdout, r.stderr
    # what the re-exec ran before: `python …/wt/__main__.py` cannot import its package
    r = subprocess.run([sys.executable, main_py, "--help"], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode != 0 and "ImportError" in r.stderr


def test_loop_reexec_keeps_dash_m_wt(tmp_path, monkeypatch):
    from test_agentic_reexec import _loop_with_commits
    _module_start(monkeypatch, "queue", "run", "--poll-interval", "5")
    _, _, calls = _loop_with_commits(tmp_path, monkeypatch, [("src/wt/x.py", "A = 3\n")])
    ((exe, argv, env),) = calls
    assert exe == sys.executable
    assert argv == [sys.executable, "-m", "wt", "queue", "run", "--poll-interval", "5"]


def test_script_start_argv(tmp_path, monkeypatch):
    exe = tmp_path / "wt"
    exe.write_text("#!/bin/sh\n")
    monkeypatch.setitem(sys.modules, "__main__", object())
    monkeypatch.setattr(sys, "argv", [str(exe), "queue", "run", "--detach"])
    assert Q.self_argv() == [sys.executable, str(exe), "queue", "run", "--detach"]
    assert Q.entry_argv() == [sys.executable, "-m", "wt", "queue", "run", "--detach"]
    exe.chmod(0o755)                                # the console script runs itself
    assert Q.entry_argv() == [str(exe), "queue", "run", "--detach"]


def test_detach_relative_log_is_made_absolute(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.chdir(tmp_path)
    calls = []

    class _Child:
        pid = 4242

        def poll(self):
            return 7

    with pytest.raises(ValueError):
        Q.detach_runner(cfg, ["wt", "queue", "run"], "rel.log",
                        popen=lambda argv, **kw: calls.append(kw) or _Child())
    assert calls[0]["env"]["WT_RUNNER_LOG"] == str(tmp_path / "rel.log")


# ---- stop: hardening ----------------------------------------------------------------------------

def test_stop_holder_gone_is_no_runner(tmp_path, children):
    cfg = _cfg(tmp_path)
    _spawn(cfg, "term", children)

    def kill(pid, sig):
        raise ProcessLookupError(pid)

    assert Q.stop_runner(cfg, kill=kill) == {"stopped": False, "reason": "no runner"}


def test_stop_permission_error_is_refused(tmp_path, children):
    cfg = _cfg(tmp_path)
    child = _spawn(cfg, "term", children)

    def kill(pid, sig):
        raise PermissionError(pid)

    with pytest.raises(ValueError, match=rf"pid {child.pid} .*cannot be signalled"):
        Q.stop_runner(cfg, kill=kill)


def test_stop_refuses_a_one_shot_tick(tmp_path):
    cfg = _cfg(tmp_path)
    with L.acquire(cfg, ["wt", "queue", "tick"]):
        with pytest.raises(ValueError, match=r"one-shot `wt queue tick`, not a runner"):
            Q.stop_runner(cfg, kill=lambda *a: pytest.fail("no SIGTERM to a tick"))


# ---- the lease file vs the flock holder (review round 2) ---------------------------------------

def _stop_with(cfg, lease, on_sleep):
    """`stop_runner` against a lease this process holds: `on_sleep(n)` runs at the n-th sleep,
    a SIGTERM (recorded) releases the lease."""
    kills, n = [], {"sleeps": 0}

    def sleep(_s):
        n["sleeps"] += 1
        on_sleep(n["sleeps"])

    def kill(pid, sig):
        kills.append(pid)
        lease.release()

    return kills, lambda: Q.stop_runner(cfg, timeout_s=5, kill=kill, sleep=sleep)


def test_stop_waits_for_a_missing_lease_file(tmp_path):
    """Between acquire's flock and its file write the flock is held but no file names a pid."""
    cfg = _cfg(tmp_path)
    lease = L.acquire(cfg, ["wt", "queue", "run"])
    L.lease_path(cfg).unlink()
    kills, stop = _stop_with(cfg, lease, lambda n: n == 1 and lease.heartbeat())
    assert stop()["stopped"] is True and kills == [os.getpid()]


def test_stop_never_signals_a_stale_record(tmp_path):
    """A SIGKILLed predecessor's file, still there while the new holder takes the flock."""
    cfg = _cfg(tmp_path)
    lease = L.acquire(cfg, ["wt", "queue", "run"])
    stale = {**lease.info, "pid": 999999, "started_at": "2026-01-01T00:00:00+00:00"}
    L.lease_path(cfg).write_text(json.dumps(stale))
    kills, stop = _stop_with(cfg, lease, lambda n: n == 1 and lease.heartbeat())
    assert stop()["stopped"] is True and kills == [os.getpid()]


def test_stop_refuses_when_the_lease_file_never_settles(tmp_path):
    cfg = _cfg(tmp_path)
    lease = L.acquire(cfg, ["wt", "queue", "run"])
    try:
        L.lease_path(cfg).unlink()
        kills, stop = _stop_with(cfg, lease, lambda n: None)
        with pytest.raises(ValueError, match=r"does not name a settled holder"):
            stop()
        assert kills == [] and L.is_held(cfg)
    finally:
        lease.release()


def test_run_launcher_gone_from_defaults(tmp_path, monkeypatch):
    """SPEC-0291 AC5: no `agentic.run_launcher` default; a user config that still sets it
    loads fine (unknown `agentic` keys are ignored)."""
    from wt import config as C
    assert "run_launcher" not in C.DEFAULT_CONFIG["agentic"]
    cdir = tmp_path / "conf"
    cdir.mkdir()
    (cdir / "config.yaml").write_text("agentic:\n  run_launcher: nohup\n")
    monkeypatch.setenv("WT_CONFIG_DIR", str(cdir))
    monkeypatch.setenv("WT_DATA_DIR", str(tmp_path / "data"))
    cfg = C.load_config()
    assert cfg["config_dir"] == str(cdir) and cfg["agentic"]["run_launcher"] == "nohup"
