"""SPEC-0225: the detached run wrapper, the run-dir contract and liveness. Uses the fake
`claude` of tests/fixtures/fake_claude.py on PATH; no real agent is ever started."""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from nightwright.agentic import runner as R
from nightwright.agentic import runproc as RP
from nightwright.agentic.personas import IMPLEMENTER_SCHEMA
from waits import join, until  # noqa: F401  (until is re-exported to the other agentic tests)

FAKE = Path(__file__).parent / "fixtures" / "fake_claude.py"


@pytest.fixture(autouse=True)
def fake_claude(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "claude").symlink_to(FAKE)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    monkeypatch.setenv("NW_RUNPROC_GRACE_S", "1")
    monkeypatch.setattr(RP, "GRACE_S", 1.0)


def mkreq(tmp_path, name="001-implementer", timeout_s=60, **env):
    (tmp_path / "nw").mkdir(exist_ok=True)
    return R.RunRequest(
        persona="implementer", prompt="do it", system_append="", cwd=tmp_path / "nw",
        model="m", effort=None, max_budget_usd=1.0, timeout_s=timeout_s,
        permission_mode="acceptEdits", read_only=False, allowed_tools=(), add_dirs=(),
        json_schema=IMPLEMENTER_SCHEMA, env=env, run_dir=tmp_path / "c" / "runs" / name)


def start_thread(runner, req, hooks=None, out=None):
    out = out if out is not None else {}
    t = threading.Thread(target=lambda: out.setdefault("res", runner.run(req, hooks=hooks)))
    t.start()
    return t, out


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return RP._proc_state(pid) != "Z"


def child_pid(req):
    """The fake child's pid. The file exists a moment before the pid is written, so poll for text."""
    path = req.cwd / "child.pid"
    assert until(lambda: path.exists() and path.read_text().strip(), interval=0.02), \
        f"{path} was never written with a pid"
    return int(path.read_text().strip())


def release(req):
    (req.cwd / "release").write_text("")


def test_worker_base_env_is_what_the_wrapper_and_claude_receive(tmp_path, monkeypatch):
    """A worker persona's `_CHILD_BASE` is PATH and HOME. The wrapper process is started
    with that plus `req.env`, and `claude` is built from `launch.json`'s `base_env`, so a
    secret in the worker process does not reach either."""
    for key, value in (("SSH_AUTH_SOCK", "/poison/agent.sock"), ("GH_TOKEN", "poison-gh"),
                       ("GITHUB_TOKEN", "poison-github"), ("GIT_ASKPASS", "/poison/askpass")):
        monkeypatch.setenv(key, value)
    dump = tmp_path / "child-env.json"
    claude = tmp_path / "bin" / "claude"
    claude.unlink()
    claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os\n"
        f"json.dump(dict(os.environ), open({str(dump)!r}, 'w'))\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,\n"
        " 'result': 'ok', 'session_id': 's', 'usage': {}, 'total_cost_usd': 0,\n"
        " 'structured_output': {'status': 'done', 'summary': 'ok'}}))\n",
        encoding="utf-8")
    claude.chmod(0o755)
    seen = {}
    real = R.subprocess.Popen

    def spy(argv, *a, **kw):
        if isinstance(argv, (list, tuple)) and "runproc" in " ".join(map(str, argv)):
            seen["env"] = kw.get("env")
        return real(argv, *a, **kw)

    monkeypatch.setattr(R.subprocess, "Popen", spy)
    base = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}
    req = mkreq(tmp_path)
    res = R.child_base_context(base).run(lambda: R.ClaudeRunner().run(req))
    assert res.verdict_ok
    forbidden = {"SSH_AUTH_SOCK", "GH_TOKEN", "GITHUB_TOKEN", "GIT_ASKPASS"}
    assert seen["env"] and not (forbidden & set(seen["env"]))
    child = json.loads(dump.read_text())
    assert not (forbidden & set(child))
    launch = json.loads((req.run_dir / "launch.json").read_text())
    assert launch["base_env"] == seen["env"]
    assert not (forbidden & set(launch["base_env"]))
    assert (req.run_dir / "launch.json").stat().st_mode & 0o777 == 0o600


def test_local_launch_omits_base_env(tmp_path, monkeypatch):
    """Local personas do not persist the process environment in `launch.json`."""
    monkeypatch.setenv("GH_TOKEN", "must-not-appear-on-disk")
    req = mkreq(tmp_path)
    assert R.ClaudeRunner().run(req).verdict_ok
    launch = json.loads((req.run_dir / "launch.json").read_text())
    assert "base_env" not in launch
    assert "GH_TOKEN" not in launch.get("env", {})


def test_spawn_writes_contract_files(tmp_path):
    req = mkreq(tmp_path, FAKE_CLAUDE_VERDICT='{"status": "done", "summary": "ok"}')
    res = R.ClaudeRunner().run(req)
    d = req.run_dir
    for name in ("prompt.md", "request.json", "launch.json", "proc.json", "child.json",
                 "result.json", "stderr.log", "exit.json", "run.json"):
        assert (d / name).is_file(), name
    assert (d / "proc.json").stat().st_mtime_ns <= (d / "child.json").stat().st_mtime_ns
    ex = json.loads((d / "exit.json").read_text())
    assert ex["exit_code"] == 0 and ex["killed_by"] is None and not ex["timed_out"]
    parsed = R.parse_result((d / "result.json").read_text(), IMPLEMENTER_SCHEMA, exit_code=0)
    assert res.verdict == parsed["verdict"] == {"status": "done", "summary": "ok"}
    assert res.cost_usd == parsed["cost_usd"] and not res.is_error and not res.interrupted
    proc, child = json.loads((d / "proc.json").read_text()), json.loads((d / "child.json").read_text())
    assert child["pgid"] == child["pid"]


def test_wrapper_timeout_without_runner(tmp_path):
    req = mkreq(tmp_path, timeout_s=1, FAKE_CLAUDE_SLEEP_S="60", FAKE_CLAUDE_STUBBORN="1")
    stop = threading.Event()
    runner = R.ClaudeRunner()
    hooks = R.RunHooks(threading.RLock(), req.run_dir)
    t, out = start_thread(runner, req, hooks)
    assert until(lambda: (req.cwd / "child.pid").exists())
    pid = child_pid(req)
    hooks.detach.set()                          # the runner thread is abandoned
    join(t)
    assert out["res"] is R.DETACHED and not stop.is_set()
    assert until((req.run_dir / "exit.json").exists, 15)
    ex = json.loads((req.run_dir / "exit.json").read_text())
    assert ex["timed_out"] and ex["killed_by"] == "timeout" and ex["signal"] == 9
    assert until(lambda: not pid_alive(pid))


def test_claim_taken_wrapper_exits(tmp_path):
    req = mkreq(tmp_path)
    req.run_dir.mkdir(parents=True)
    (req.run_dir / "proc.json").write_text(json.dumps({"orphaned": True}))
    r = subprocess.run([sys.executable, "-m", "nightwright.agentic.runproc", str(req.run_dir)],
                       capture_output=True, text=True, timeout=20)
    assert r.returncode != 0 and not (req.cwd / "child.pid").exists()
    assert json.loads((req.run_dir / "proc.json").read_text()) == {"orphaned": True}


def test_attach_existing_proc(tmp_path):
    req = mkreq(tmp_path)
    assert R.ClaudeRunner().run(req).verdict_ok
    before = (req.run_dir / "proc.json").read_text()
    pid_before = (req.cwd / "child.pid").read_text()
    res = R.ClaudeRunner().run(req)
    assert res.verdict_ok and (req.run_dir / "proc.json").read_text() == before
    assert (req.cwd / "child.pid").read_text() == pid_before       # nothing was spawned


def test_detached_waiter_returns_fast(tmp_path):
    req = mkreq(tmp_path, FAKE_CLAUDE_SLEEP_S="60")
    hooks = R.RunHooks(threading.RLock(), req.run_dir)
    t, out = start_thread(R.ClaudeRunner(), req, hooks)
    assert until(lambda: hooks.phase == "wait" and (req.cwd / "child.pid").exists())
    proc, child = RP.read_json(req.run_dir / "proc.json"), RP.read_json(req.run_dir / "child.json")
    assert os.getsid(proc["wrapper_pid"]) != os.getsid(0)          # the wrapper has its own session
    assert os.getpgid(child["pid"]) == child["pgid"] != os.getpgid(proc["wrapper_pid"])
    t0 = time.monotonic()
    hooks.detach.set()
    join(t)
    assert out["res"] is R.DETACHED and time.monotonic() - t0 < 5.0
    release(req)
    assert until((req.run_dir / "exit.json").exists)
    time.sleep(0.5)
    assert not (req.run_dir / "run.json").exists()                 # the detached thread wrote nothing


def test_orphan_group_waited_and_killed_at_timeout(tmp_path):
    req = mkreq(tmp_path, timeout_s=2, FAKE_CLAUDE_SLEEP_S="60")
    hooks = R.RunHooks(threading.RLock(), req.run_dir)
    t, out = start_thread(R.ClaudeRunner(), req, hooks)
    assert until(lambda: (req.run_dir / "child.json").exists() and (req.cwd / "child.pid").exists())
    proc = json.loads((req.run_dir / "proc.json").read_text())
    os.kill(proc["wrapper_pid"], signal.SIGKILL)
    assert until(lambda: RP.run_status(req.run_dir) == "orphan_group")
    assert RP.group_alive(RP.read_json(req.run_dir / "child.json"))
    pid = child_pid(req)
    join(t)
    res = out["res"]
    ex = json.loads((req.run_dir / "exit.json").read_text())
    assert ex["synthesized"] and ex["killed_by"] == "timeout" and res.timed_out
    assert not pid_alive(pid)


def test_orphan_group_finishing_on_its_own_is_synthesized(tmp_path):
    """SIGKILL only the wrapper, then let `claude` finish: the result is not lost."""
    req = mkreq(tmp_path, FAKE_CLAUDE_SLEEP_S="60", FAKE_CLAUDE_COST="0.25",
                FAKE_CLAUDE_VERDICT='{"status": "done", "summary": "ok"}')
    hooks = R.RunHooks(threading.RLock(), req.run_dir)
    t, out = start_thread(R.ClaudeRunner(), req, hooks)
    assert until(lambda: (req.run_dir / "child.json").exists() and (req.cwd / "child.pid").exists())
    os.kill(json.loads((req.run_dir / "proc.json").read_text())["wrapper_pid"], signal.SIGKILL)
    assert until(lambda: RP.run_status(req.run_dir) == "orphan_group")
    time.sleep(0.5)                                  # the wait loop has seen the orphan group
    release(req)
    join(t)
    res = out["res"]
    ex = json.loads((req.run_dir / "exit.json").read_text())
    assert ex["synthesized"] and ex["exit_code"] == 0 and ex["killed_by"] is None
    assert not res.is_error and not res.interrupted and res.cost_usd == 0.25
    assert res.verdict and res.verdict["summary"] == "ok"


def test_term_handled_by_child_is_still_signal(tmp_path):
    """`claude` catches the forwarded TERM and exits normally: still `killed_by: signal`."""
    req = mkreq(tmp_path, FAKE_CLAUDE_SLEEP_S="60", FAKE_CLAUDE_TERM_EXIT="143")
    hooks = R.RunHooks(threading.RLock(), req.run_dir)
    t, out = start_thread(R.ClaudeRunner(), req, hooks)
    assert until(lambda: (req.run_dir / "child.json").exists() and (req.cwd / "child.pid").exists())
    os.kill(json.loads((req.run_dir / "proc.json").read_text())["wrapper_pid"], signal.SIGTERM)
    join(t)
    ex = json.loads((req.run_dir / "exit.json").read_text())
    assert ex["killed_by"] == "signal" and ex["signal"] == 15 and ex["exit_code"] == 143
    assert out["res"].interrupted


def test_orphan_group_stopped_is_interrupted(tmp_path):
    """`terminate_run` on a run whose wrapper is dead TERMs the group; it collects interrupted."""
    req = mkreq(tmp_path, FAKE_CLAUDE_SLEEP_S="60")
    hooks = R.RunHooks(threading.RLock(), req.run_dir)
    t, out = start_thread(R.ClaudeRunner(), req, hooks)
    assert until(lambda: (req.run_dir / "child.json").exists() and (req.cwd / "child.pid").exists())
    pid = child_pid(req)
    os.kill(json.loads((req.run_dir / "proc.json").read_text())["wrapper_pid"], signal.SIGKILL)
    assert until(lambda: RP.run_status(req.run_dir) == "orphan_group")
    assert RP.terminate_run(req.run_dir)
    join(t)
    assert not pid_alive(pid)
    assert out["res"].interrupted


def test_vanished_when_group_gone(tmp_path):
    req = mkreq(tmp_path, FAKE_CLAUDE_SLEEP_S="60")
    hooks = R.RunHooks(threading.RLock(), req.run_dir)
    t, out = start_thread(R.ClaudeRunner(), req, hooks)
    assert until(lambda: (req.run_dir / "child.json").exists() and (req.cwd / "child.pid").exists())
    proc = json.loads((req.run_dir / "proc.json").read_text())
    child = json.loads((req.run_dir / "child.json").read_text())
    # SIGSTOP keeps the wrapper "alive" (state T) while the group dies, so the runner's poll
    # sees running -> vanished and never orphan_group, whatever the timing. A stopped wrapper
    # cannot reap the child; group_alive already treats an all-zombie group as gone.
    os.kill(proc["wrapper_pid"], signal.SIGSTOP)
    try:
        os.killpg(child["pgid"], signal.SIGKILL)
        assert until(lambda: not RP.group_alive(child))
    finally:
        os.kill(proc["wrapper_pid"], signal.SIGKILL)
    join(t)
    res = out["res"]
    assert res.exit_code == -1 and res.is_error and not res.interrupted
    assert res.cost_usd is None and "vanished" in res.result_text
    assert until(lambda: not pid_alive(child["pid"]))


def test_pid_reuse_ignored(tmp_path):
    me = os.getpid()
    assert RP.wrapper_alive({"wrapper_pid": me, "wrapper_start_ticks": RP.start_ticks(me)})
    assert not RP.wrapper_alive({"wrapper_pid": me, "wrapper_start_ticks": 1})
    pgid = os.getpgid(0)
    assert RP.group_alive({"pid": me, "pgid": pgid, "start_ticks": RP.start_ticks(me)})
    assert not RP.group_alive({"pid": me, "pgid": pgid, "start_ticks": 1})


def test_killed_by_signal_is_interrupted(tmp_path):
    req = mkreq(tmp_path, FAKE_CLAUDE_SLEEP_S="60")
    hooks = R.RunHooks(threading.RLock(), req.run_dir)
    t, out = start_thread(R.ClaudeRunner(), req, hooks)
    assert until(lambda: (req.run_dir / "child.json").exists())
    os.kill(json.loads((req.run_dir / "proc.json").read_text())["wrapper_pid"], signal.SIGTERM)
    join(t)
    ex = json.loads((req.run_dir / "exit.json").read_text())
    assert ex["killed_by"] == "signal" and out["res"].interrupted

    req = mkreq(tmp_path, name="002-implementer", FAKE_CLAUDE_SLEEP_S="60")
    hooks = R.RunHooks(threading.RLock(), req.run_dir)
    t, out = start_thread(R.ClaudeRunner(), req, hooks)
    assert until(lambda: (req.run_dir / "child.json").exists())
    os.kill(json.loads((req.run_dir / "child.json").read_text())["pid"], signal.SIGKILL)
    join(t)
    ex = json.loads((req.run_dir / "exit.json").read_text())
    res = out["res"]
    assert ex["killed_by"] == "external" and res.is_error and not res.interrupted


def test_spawn_timeout_failed_launch(tmp_path, monkeypatch):
    req = mkreq(tmp_path)
    real_popen = subprocess.Popen

    def slow_wrapper(argv, **kw):                 # the wrapper hangs before it can claim
        if "nightwright.agentic.runproc" in argv:
            argv = [sys.executable, "-c", "import time, sys; print('hung-wrapper-marker', flush=True);"
                    "time.sleep(float(sys.argv[1]));"
                    "import runpy; sys.argv = ['x', sys.argv[2]];"
                    "runpy.run_module('nightwright.agentic.runproc', run_name='__main__')",
                    "3", argv[-1]]
        return real_popen(argv, **kw)

    monkeypatch.setattr(R.subprocess, "Popen", slow_wrapper)
    t0 = time.monotonic()
    res = R.ClaudeRunner(spawn_timeout_s=1).run(req)
    assert time.monotonic() - t0 < 3 and res.is_error and res.exit_code == -1
    assert "launch failed" in res.result_text
    assert "hung-wrapper-marker" in res.result_text            # the wrapper.log tail
    proc = json.loads((req.run_dir / "proc.json").read_text())
    assert proc["orphaned"] and proc["by"] == "spawn_timeout"
    time.sleep(3.5)                                # the late wrapper loses the claim
    assert not (req.cwd / "child.pid").exists() and not (req.run_dir / "child.json").exists()


def test_wrapper_dying_before_claim_returns_promptly(tmp_path, monkeypatch):
    req = mkreq(tmp_path)
    real_popen = subprocess.Popen

    def dead_wrapper(argv, **kw):
        if "nightwright.agentic.runproc" in argv:
            argv = [sys.executable, "-c", "raise SystemExit('boom')"]
        return real_popen(argv, **kw)

    monkeypatch.setattr(R.subprocess, "Popen", dead_wrapper)
    t0 = time.monotonic()
    res = R.ClaudeRunner(spawn_timeout_s=30).run(req)
    assert time.monotonic() - t0 < 5 and res.is_error and "boom" in res.result_text


def test_claude_popen_failure(tmp_path, monkeypatch):
    # Hermetic: a real `claude` on the host (~/.local/bin, /usr/bin) would be found through
    # SPEC-0389's fallback dirs and run, so empty those lookups too.
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))  # SPEC-0389: no ~/.local/bin/claude fallback
    monkeypatch.setattr(RP, "_CLAUDE_SYSTEM", ())
    req = mkreq(tmp_path)
    res = R.ClaudeRunner().run(req)
    ex = json.loads((req.run_dir / "exit.json").read_text())
    assert ex["exit_code"] == -1 and ex["error"]
    assert res.is_error and res.exit_code == -1 and "claude" in res.result_text


def test_attached_run_killed_on_keyboard_interrupt(tmp_path, monkeypatch):
    req = mkreq(tmp_path, FAKE_CLAUDE_SLEEP_S="60")
    runner = R.ClaudeRunner()
    calls = []

    def poll():
        calls.append(1)
        if (req.cwd / "child.pid").exists():
            raise KeyboardInterrupt
        time.sleep(0.05)

    monkeypatch.setattr(runner, "_poll", poll)
    with pytest.raises(KeyboardInterrupt):
        runner.run(req)
    pid = child_pid(req)
    proc = json.loads((req.run_dir / "proc.json").read_text())
    assert until(lambda: not pid_alive(pid))
    assert until(lambda: not pid_alive(proc["wrapper_pid"]))


def test_wrapper_log_and_unknown_keys(tmp_path):
    req = mkreq(tmp_path)
    runner = R.ClaudeRunner()
    _orig = R._request_doc
    # an extra key in launch.json / request.json is ignored
    req.run_dir.mkdir(parents=True)
    (req.run_dir / "prompt.md").write_text("p")
    RP.write_json_atomic(req.run_dir / "request.json", {**_orig(req, "t"), "future": 1})
    RP.write_json_atomic(req.run_dir / "launch.json", {
        "argv": ["claude", "-p"], "cwd": str(req.cwd), "env": {}, "future": [1]})
    assert subprocess.run([sys.executable, "-m", "nightwright.agentic.runproc", str(req.run_dir)],
                          timeout=20).returncode == 0
    assert json.loads((req.run_dir / "exit.json").read_text())["exit_code"] == 0
    # a broken launch.json leaves a traceback in wrapper.log and returns promptly
    bad = mkreq(tmp_path, name="002-implementer")
    bad.run_dir.mkdir(parents=True)
    (bad.run_dir / "prompt.md").write_text("p")
    (bad.run_dir / "request.json").write_text("{}")
    (bad.run_dir / "launch.json").write_text("{not json")
    with open(bad.run_dir / "wrapper.log", "w") as log:
        subprocess.run([sys.executable, "-m", "nightwright.agentic.runproc", str(bad.run_dir)],
                       stdout=log, stderr=log, timeout=20)
    assert "Traceback" in (bad.run_dir / "wrapper.log").read_text()
    t0 = time.monotonic()
    res = runner.run(bad)
    assert time.monotonic() - t0 < 5 and res.is_error


def test_claim_never_exposes_partial_proc_json(tmp_path):
    """`claim` links a fully written file into place: a reader polling for `proc.json` never sees
    it empty, a second claim loses, and no temp file is left behind."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    seen, stop = [], threading.Event()

    def poll():
        while not stop.is_set():
            try:
                seen.append((run_dir / "proc.json").read_text())
            except FileNotFoundError:
                pass

    t = threading.Thread(target=poll)
    t.start()
    try:
        assert RP.claim(run_dir, {"wrapper_pid": 1, "pad": "x" * 100000})
        time.sleep(0.05)
    finally:
        stop.set()
        t.join()
    assert seen and all(json.loads(s)["wrapper_pid"] == 1 for s in seen)
    assert not RP.claim(run_dir, {"orphaned": True})
    assert RP.read_json(run_dir / "proc.json")["wrapper_pid"] == 1
    assert [p.name for p in run_dir.iterdir()] == ["proc.json"]


def test_wrapper_failure_after_popen_kills_child(tmp_path, monkeypatch):
    """SPEC-0262: a wrapper that fails after `Popen` ends the child's group before it writes
    `exit.json`, and says so in `exit.json.error`."""
    import errno
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "launch.json").write_text(json.dumps({"argv": ["sleep", "30"],
                                                     "cwd": str(tmp_path)}))
    (run_dir / "request.json").write_text(json.dumps({"timeout_s": 60}))
    (run_dir / "prompt.md").write_text("")
    monkeypatch.setenv("NW_RUNPROC_GRACE_S", "0.5")
    monkeypatch.setattr(RP, "GRACE_S", 0.5)
    real_write, seen = RP.write_json_atomic, {}

    def failing_write(path, data):
        if Path(path).name == "child.json":
            seen.update(data)
            raise OSError(errno.ENOSPC, "No space left on device")
        real_write(path, data)

    monkeypatch.setattr(RP, "write_json_atomic", failing_write)
    prev = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}
    try:
        assert RP.main([str(run_dir)]) == 1
    finally:
        for s, h in prev.items():
            signal.signal(s, h)
    ex = json.loads((run_dir / "exit.json").read_text())
    assert ex["exit_code"] == -1 and ex["error"].endswith("child process group killed")

    def gone():
        try:
            os.killpg(seen["pgid"], 0)
        except ProcessLookupError:
            return True
        return False

    assert until(gone, timeout=10.0)
