"""SPEC-0392: a gate's timeout_s is a hard bound. The command's whole process tree (SIGTERM-ignoring
descendants, a setsid'd child) is gone within `timeout_s + 2 x grace + reap bound`, and `run_gate`
always returns. The fixture builds the shape of the `uv run pytest -n 4` incident without uv."""
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from nightwright.agentic import gates as G
import waits

LINUX = Path("/proc/self/stat").exists()
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not LINUX, reason='process-tree kill needs /proc')]


def _gate(name, cmd, *, timeout_s=1):
    return {"name": name, "cmd": cmd, "cwd": ".", "timeout_s": timeout_s, "required": True}


def _alive(pid) -> bool:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return raw.rsplit(")", 1)[1].split()[0] != "Z"          # a zombie is dead, only unreaped


def _pids(path: Path) -> list[int]:
    return [int(x) for x in path.read_text().split()] if path.exists() else []


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setenv("NW_GATE_GRACE_S", "0.3")
    monkeypatch.setenv("NW_GATE_REAP_S", "2")


@pytest.fixture
def tree_cmd(tmp_path):
    """A parent that forks 4 SIGTERM-ignoring children, one in a session of its own (setsid)."""
    pidfile = tmp_path / "pids"
    script = tmp_path / "tree.py"
    script.write_text(textwrap.dedent(f"""
        import signal, subprocess, time
        body = 'trap "" TERM; echo $$ >> {pidfile}; while :; do sleep 1; done'
        for i in range(4):
            subprocess.Popen(["sh", "-c", body], start_new_session=(i == 3))
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(600)
    """))
    return f"{sys.executable} {script}", pidfile


def _reap(pids):
    for p in pids:
        try:
            os.kill(p, 9)
        except OSError:
            pass


def test_timeout_kills_sigterm_ignoring_descendants_and_the_setsid_child(
        tmp_path, fast, tree_cmd):
    cmd, pidfile = tree_cmd
    t0 = time.monotonic()
    try:
        r = G.run_gate(_gate("tree", cmd), tmp_path, {}, tmp_path / "tree.log", {})
        took = time.monotonic() - t0
        assert waits.until(lambda: len(_pids(pidfile)) == 4, 5)
        survivors = [p for p in _pids(pidfile) if _alive(p)]
        assert survivors == [], f"descendants survived the timeout kill: {survivors}"
    finally:
        _reap(_pids(pidfile))
    assert r.timed_out and not r.ok
    assert took < 1 + 2 * 0.3 + 2 + 1                      # timeout + 2 x grace + reap bound + 1 s
    side = json.loads((tmp_path / "tree.json").read_text())
    assert side["timed_out"] and side["error"].startswith("timed out after 1s")
    assert "survivors" not in side["error"]


def test_kill_group_sigkills_descendants_after_the_shell_already_exited(tmp_path, fast):
    pidfile = tmp_path / "pid"
    cmd = f"sh -c 'trap \"\" TERM; echo $$ > {pidfile}; while :; do sleep 1; done' & wait"
    proc = subprocess.Popen(cmd, shell=True, start_new_session=True)
    try:
        assert waits.until(lambda: bool(_pids(pidfile)), 10)
        child = _pids(pidfile)[0]
        G._kill_group(proc)
        assert proc.poll() is not None                      # the shell died on the TERM...
        assert not _alive(child)                            # ...its TERM-proof child on the KILL
    finally:
        _reap(_pids(pidfile))
        proc.kill()
        proc.wait()


def test_run_gate_returns_by_the_hard_deadline_when_the_kill_never_completes(
        tmp_path, monkeypatch):
    monkeypatch.setenv("NW_GATE_GRACE_S", "0.2")
    monkeypatch.setenv("NW_GATE_REAP_S", "0.2")
    monkeypatch.setattr(G, "HARD_SLACK_S", 0.3)
    release = threading.Event()
    monkeypatch.setattr(G, "_kill_tree", lambda proc: release.wait(30))
    t0 = time.monotonic()
    try:
        r = G.run_gate(_gate("wedged", "sleep 4"), tmp_path, {}, tmp_path / "w.log", {})
    finally:
        release.set()
    took = time.monotonic() - t0
    assert took < 1 + G.hard_deadline_s() + 1               # timeout_s + HARD_DEADLINE_S (+ 1 s)
    assert r.timed_out and r.exit_code is None and not r.ok
    side = json.loads((tmp_path / "w.json").read_text())
    assert "kill did not complete" in side["error"] and side["timed_out"]
    assert G.live_deadlines() == []


def test_a_recycled_pid_is_not_signalled(tmp_path, fast, monkeypatch):
    proc = subprocess.Popen("sleep 30", shell=True, start_new_session=True)
    sent = []
    try:
        assert waits.until(lambda: Path(f"/proc/{proc.pid}/stat").exists(), 5)
        monkeypatch.setattr(G.runproc, "start_ticks", lambda pid: -1)   # "the pid was recycled"
        monkeypatch.setattr(G.os, "kill", lambda pid, sig: sent.append(("kill", pid, sig)))
        monkeypatch.setattr(G.os, "killpg", lambda pg, sig: sent.append(("killpg", pg, sig)))
        G._kill_tree(proc)
    finally:
        monkeypatch.undo()
        proc.kill()
        proc.wait()
    assert sent == []


def test_a_worker_job_style_call_goes_through_the_same_kill(tmp_path, fast, tree_cmd):
    """`worker.py` runs gates with `run_gate_with_retries`, which calls `run_gate` (read at
    gates.py `pre = run_gate(...)` / the ladder), so the worker box gets the same tree kill."""
    cmd, pidfile = tree_cmd
    try:
        res = G.run_gate_with_retries(_gate("tree", cmd), tmp_path, {}, tmp_path / "t.log", {})
        assert waits.until(lambda: len(_pids(pidfile)) == 4, 5)
        assert [p for p in _pids(pidfile) if _alive(p)] == []
    finally:
        _reap(_pids(pidfile))
    final = res[-1] if isinstance(res, (list, tuple)) else res
    assert final.timed_out


def test_real_pytest_xdist_under_a_timeout(tmp_path, fast):
    pytest.importorskip("xdist")
    (tmp_path / "test_sleep.py").write_text(
        "import time\n\ndef test_a():\n    time.sleep(120)\n\ndef test_b():\n    time.sleep(120)\n")
    cmd = f"{sys.executable} -m pytest -q -p no:cacheprovider -n 2 test_sleep.py"
    r = G.run_gate(_gate("xdist", cmd, timeout_s=3), tmp_path, {}, tmp_path / "x.log", {})
    assert r.timed_out
    out = subprocess.run(["pgrep", "-f", str(tmp_path / "test_sleep.py")],
                         capture_output=True, text=True).stdout.split()
    assert waits.until(lambda: not any(_alive(int(p)) for p in out), 5), out
