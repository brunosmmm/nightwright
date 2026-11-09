"""SPEC-0365: the conductor follower auto-starts `nw herdr watch` — gate, state file with start
ticks, the supervisor (one child, backoff, waits for another watcher, stops only its own child,
best effort), and real follower processes against a fake data dir and the fake `herdr`.

Nothing here reaches a real herdr: conftest strips every ambient `HERDR_*` and the `fake`
fixture puts only the fake's directory (plus the interpreter's) on PATH."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from nightwright.agentic import herdr_autowatch as AW
from nightwright.cli import cli
import waits
from test_agentic_events import _cfg

pytestmark = pytest.mark.fast

FAKE_SRC = Path(__file__).parent / "fixtures" / "fake_herdr.py"


@pytest.fixture
def fake(tmp_path, monkeypatch):
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    exe = bindir / "herdr"
    exe.write_text(f"#!{sys.executable}\n" + FAKE_SRC.read_text())
    exe.chmod(0o755)
    log = tmp_path / "herdr.log"
    monkeypatch.setenv("FAKE_HERDR_LOG", str(log))
    monkeypatch.setenv("FAKE_HERDR_MODE", "ok")
    monkeypatch.setenv("FAKE_HERDR_STATE", str(tmp_path / "herdr-state.json"))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{Path(sys.executable).parent}")

    class F:
        path = log

        @staticmethod
        def calls():
            return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
    return F


def _herdr_env(**kw):
    return {"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p1", "HERDR_WORKSPACE_ID": "w1",
            "HERDR_TAB_ID": "w1:t1", **kw}


# ---- gate / config / state -----------------------------------------------------------------

def test_gate_and_config(tmp_path, fake):
    cfg = _cfg(tmp_path)
    env = {**os.environ, **_herdr_env()}
    assert AW.gate(cfg, env) is None
    for off in (False, 0, "false", "off", "no", "0"):
        assert "auto_watch" in AW.gate({**cfg, "agentic": {"herdr": {"auto_watch": off}}}, env)
    for on in (True, "true", 1, None):
        assert AW.gate({**cfg, "agentic": {"herdr": {"auto_watch": on}}}, env) is None
    assert "HERDR_ENV" in AW.gate(cfg, {**env, "HERDR_ENV": "0"})
    assert "PATH" in AW.gate(cfg, {**env, "PATH": str(tmp_path / "nowhere")})


def test_state_file_liveness_and_stale_pids(tmp_path):
    cfg = _cfg(tmp_path)
    assert AW.status(cfg)["running"] is False
    rec = AW.write_state(cfg, "manual")
    st = AW.status(cfg)
    assert st["running"] and st["pid"] == os.getpid() and st["mode"] == "manual"
    assert st["log"] == str(AW.log_path(cfg)) and st["schema"] == "nw.herdr.watch.v1"
    # same pid, different start time: a reused pid is not the watcher
    AW.state_path(cfg).write_text(json.dumps({**rec, "start_ticks": rec["start_ticks"] + 1}))
    assert AW.status(cfg)["running"] is False
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    AW.state_path(cfg).write_text(json.dumps({**rec, "pid": p.pid}))
    assert AW.status(cfg)["running"] is False
    AW.clear_state(cfg)                          # not ours: left alone
    assert AW.state_path(cfg).exists()
    AW.write_state(cfg, "auto", (123, 456))
    assert AW.read_state(cfg)["parent_pid"] == 123
    AW.clear_state(cfg)
    assert not AW.state_path(cfg).exists()
    assert AW.parse_parent("12:34") == (12, 34) and AW.parse_parent("12") == (12, None)
    assert AW.parse_parent("x:1") is None


# ---- supervisor with a fake popen ----------------------------------------------------------

class FakeChild:
    n = 1000

    def __init__(self):
        FakeChild.n += 1
        self.pid = FakeChild.n
        self.rc = None
        self.signals = []

    def poll(self):
        return self.rc

    def send_signal(self, sig):
        self.signals.append(sig)


class Rig:
    def __init__(self, tmp_path, holder=False):
        self.t = 0.0
        self.children = []
        self.kw = []
        self.holder = holder
        self.cfg = _cfg(tmp_path)
        self.sup = AW.Supervisor(self.cfg, env={"HERDR_ENV": "1"}, clock=lambda: self.t,
                                 popen=self.popen, holder=lambda: self.holder)
        self.sup.active = True                   # the gate is tested above

    def popen(self, argv, **kw):
        c = FakeChild()
        self.children.append(c)
        self.kw.append((argv, kw))
        return c

    def at(self, t):
        self.t = t
        self.sup.tick()


def test_supervisor_one_child_backoff_and_reset(tmp_path):
    r = Rig(tmp_path)
    r.at(0)
    r.at(1)
    r.at(2)
    assert len(r.children) == 1
    argv, kw = r.kw[0]
    assert argv[1:6] == ["-m", "nightwright", "herdr", "watch", "--parent"]
    assert argv[6].startswith(f"{os.getpid()}:")
    assert kw["stdin"] == subprocess.DEVNULL and kw["stderr"] == subprocess.STDOUT
    assert isinstance(kw["stdout"], int) and kw["close_fds"] and kw["start_new_session"]
    assert kw["env"]["NW_DATA_DIR"] == r.cfg["data_dir"]
    starts = []
    for _ in range(8):                           # crash right away, every time
        r.children[-1].rc = 1
        r.at(r.t + 0.1)                          # noticed: next start after the backoff
        before = len(r.children)
        t0 = r.t
        while len(r.children) == before:
            r.at(r.t + 1)
        starts.append(round(r.t - t0))
    assert starts == [5, 10, 20, 40, 80, 160, 300, 300]
    r.at(r.t + AW.STABLE_S + 1)                  # a long, healthy run resets the backoff
    r.children[-1].rc = 0
    r.at(r.t + 0.1)
    t0 = r.t
    n = len(r.children)
    while len(r.children) == n:
        r.at(r.t + 1)
    assert round(r.t - t0) == 5
    log = AW.log_path(r.cfg).read_text()
    assert "started watcher pid" in log and "exited 1" in log


def test_supervisor_waits_for_another_watcher_and_never_signals_it(tmp_path):
    r = Rig(tmp_path, holder=True)
    for t in range(0, 30):
        r.at(t)
    assert r.children == []
    assert AW.log_path(r.cfg).read_text().count("already runs") == 1
    r.holder = False
    r.at(30)
    r.at(31)
    r.at(36)
    assert len(r.children) == 1
    # a live state file counts as "another watcher" too
    r2 = Rig(tmp_path / "b")
    AW.write_state(r2.cfg, "manual")
    r2.at(0)
    assert r2.children == []
    r2.sup.stop()                                # no child of ours: nothing signalled


def test_stop_sigterms_only_our_live_child(tmp_path):
    r = Rig(tmp_path)
    r.at(0)
    c = r.children[0]
    r.sup.stop()
    assert c.signals == [signal.SIGTERM]
    r.sup.tick()                                 # stopped: inert
    assert len(r.children) == 1
    r2 = Rig(tmp_path / "b")
    r2.at(0)
    r2.children[0].rc = 0
    r2.sup.stop()                                # already exited: not signalled
    assert r2.children[0].signals == []


def test_errors_never_raise_and_turn_the_supervisor_off(tmp_path, capsys):
    r = Rig(tmp_path)

    def boom(argv, **kw):
        raise OSError("no exec")
    r.sup.popen = boom
    t = 0
    while r.sup.active and t < 10_000:
        r.at(t)
        t += 1
    assert not r.sup.active and r.sup.errors == AW.ERRORS_MAX
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""
    assert "errors in a row" in AW.log_path(r.cfg).read_text()
    # an unwritable log is swallowed too
    r3 = Rig(tmp_path / "c")
    (Path(r3.cfg["data_dir"]) / "agentic" / "conductor").mkdir(parents=True)
    AW.log_path(r3.cfg).mkdir()                  # a directory where the log should be
    r3.at(0)
    r3.sup.stop()
    assert capsys.readouterr().out == ""


def test_hook_ticks_and_never_stops(tmp_path):
    r = Rig(tmp_path)
    hook = r.sup.as_stop_hook()
    assert hook() is False and len(r.children) == 1


def test_gated_supervisor_is_inert(tmp_path, fake):
    cfg = {**_cfg(tmp_path), "agentic": {"herdr": {"auto_watch": False}}}
    sup = AW.Supervisor(cfg, env={**os.environ, **_herdr_env()},
                        popen=lambda *a, **k: pytest.fail("spawned"))
    assert sup.start() is False
    sup.tick()
    sup.stop()
    assert not AW.log_path(cfg).exists() and not AW.state_path(cfg).exists()


# ---- CLI: status ---------------------------------------------------------------------------

def test_status_commands(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["herdr", "status", "--json"])
    assert r.exit_code == 0, r.output
    st = json.loads(r.output)
    assert st["running"] is False and st["log"].endswith("agentic/conductor/herdr_watch.log")
    AW.write_state(cfg, "auto", (1, None))
    r = CliRunner().invoke(cli, ["herdr", "status"])
    assert f"pid {os.getpid()} (auto)" in r.output
    from nightwright.agentic.cli_conduct import conduct_status
    hw = conduct_status(cfg)["herdr_watch"]
    assert hw["running"] and hw["pid"] == os.getpid() and hw["mode"] == "auto"


# ---- real processes ------------------------------------------------------------------------

def _env(tmp_path, cfg, **extra):
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "config.yaml").write_text(json.dumps(
        {"agentic": cfg.get("agentic") or {}, "specs_dir": cfg["specs_dir"]}))
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
    env.update(NW_CONFIG_DIR=str(tmp_path / "config"), NW_DATA_DIR=cfg["data_dir"],
               PYTHONUNBUFFERED="1", **extra)
    return env


def _follower(env):
    return subprocess.Popen([sys.executable, "-m", "nightwright", "queue", "events", "--follow",
                             "--for", "conductor", "--poll", "0.2", "--no-code-watch"],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, start_new_session=True)


def _auto_state(cfg, parent):
    st = AW.live_state(cfg)
    return st if st and st.get("mode") == "auto" and st.get("parent_pid") == parent else None


def _gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:                                         # a zombie child of ours is gone too
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
    except OSError:
        return True


def _kill(p):
    if p.poll() is None:
        p.kill()
    p.communicate(timeout=30)


def _watchers(follower_pid):
    """Live (non-zombie) `nw herdr watch --parent <follower_pid>:…` processes, from /proc."""
    want = f"herdr\0watch\0--parent\0{follower_pid}:"
    out = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            if want in (d / "cmdline").read_bytes().decode(errors="replace") \
                    and not _gone(int(d.name)):
                out.append(int(d.name))
        except OSError:
            continue
    return sorted(out)


def test_real_follower_starts_one_watcher_restarts_it_and_stops_it(tmp_path, fake):
    cfg = _cfg(tmp_path)
    f = _follower(_env(tmp_path, cfg, **_herdr_env()))
    try:
        assert waits.until(lambda: _auto_state(cfg, f.pid), timeout=60), \
            AW.log_path(cfg).read_text() if AW.log_path(cfg).exists() else "no log"
        w = _auto_state(cfg, f.pid)["pid"]
        assert _watchers(f.pid) == [w]
        # it pushes to the conductor pane the follower recorded
        assert waits.until(lambda: any(c[:3] == ["pane", "report-metadata", "w1:p1"]
                                       and "--display-agent" in c for c in fake.calls()))
        # no fd of the watcher is the follower's stdout pipe
        pipe = os.readlink(f"/proc/{f.pid}/fd/1")
        fds = {os.readlink(f"/proc/{w}/fd/{n}") for n in os.listdir(f"/proc/{w}/fd")}
        assert pipe.startswith("pipe:") and pipe not in fds
        assert os.readlink(f"/proc/{w}/fd/1") == str(AW.log_path(cfg))
        # crash -> restarted after the backoff, still exactly one
        os.kill(w, signal.SIGKILL)
        assert waits.until(lambda: (_auto_state(cfg, f.pid) or {}).get("pid") not in (None, w),
                           timeout=60)
        w2 = _auto_state(cfg, f.pid)["pid"]
        assert _watchers(f.pid) == [w2]
        n_push = sum("--display-agent" in c for c in fake.calls())
        assert waits.until(lambda: sum("--display-agent" in c for c in fake.calls()) > n_push)
        n_clear = sum("--clear-display-agent" in c for c in fake.calls())
        f.send_signal(signal.SIGINT)             # a normal follower exit
        out, err = f.communicate(timeout=60)
        assert f.returncode == 0, err
        assert out == b"" and b"herdr" not in err
        assert waits.until(lambda: _gone(w2))
        assert waits.until(lambda: not AW.state_path(cfg).exists())
        assert sum("--clear-display-agent" in c for c in fake.calls()) > n_clear
        log = AW.log_path(cfg).read_text()
        assert log.count("started watcher pid") == 2 and "stopped watcher pid" in log
    finally:
        _kill(f)
        for pid in _watchers(f.pid):
            os.kill(pid, signal.SIGKILL)


def test_watcher_exits_when_its_follower_is_killed(tmp_path, fake):
    cfg = _cfg(tmp_path)
    f = _follower(_env(tmp_path, cfg, **_herdr_env()))
    try:
        assert waits.until(lambda: _auto_state(cfg, f.pid), timeout=60)
        w = _auto_state(cfg, f.pid)["pid"]
        assert waits.until(lambda: any("--display-agent" in c for c in fake.calls()))
        f.kill()
        f.communicate(timeout=30)
        t0 = time.monotonic()
        assert waits.until(lambda: _gone(w))
        assert time.monotonic() - t0 < 10           # within one push interval
        assert waits.until(lambda: not AW.state_path(cfg).exists())
        assert any("--clear-display-agent" in c for c in fake.calls())
        assert "is gone; exiting" in AW.log_path(cfg).read_text()
    finally:
        _kill(f)


def test_manual_watcher_is_not_doubled_nor_killed(tmp_path, fake):
    cfg = _cfg(tmp_path)
    env = _env(tmp_path, cfg, **_herdr_env())
    manual = subprocess.Popen([sys.executable, "-m", "nightwright", "herdr", "watch"], env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              start_new_session=True)
    f = None
    try:
        assert waits.until(lambda: (AW.live_state(cfg) or {}).get("mode") == "manual")
        f = _follower(env)
        assert waits.until(lambda: AW.log_path(cfg).exists()
                           and "already runs" in AW.log_path(cfg).read_text())
        assert _watchers(f.pid) == [] and manual.poll() is None
        manual.send_signal(signal.SIGTERM)
        manual.communicate(timeout=60)
        assert waits.until(lambda: _auto_state(cfg, f.pid), timeout=60)
        f.send_signal(signal.SIGINT)
        out, _ = f.communicate(timeout=60)
        assert out == b""
    finally:
        _kill(manual)
        if f is not None:
            _kill(f)
            for pid in _watchers(f.pid):
                os.kill(pid, signal.SIGKILL)


@pytest.mark.parametrize("case", ["no_herdr_env", "auto_off", "steward"])
def test_no_watcher_when_gated_or_not_conductor(tmp_path, fake, case):
    cfg = _cfg(tmp_path)
    extra = {} if case == "no_herdr_env" else _herdr_env()
    if case == "auto_off":
        cfg["agentic"] = {**cfg["agentic"], "herdr": {"auto_watch": False}}
    env = _env(tmp_path, cfg, **extra)
    reader = "steward-x" if case == "steward" else "conductor"
    f = subprocess.Popen([sys.executable, "-m", "nightwright", "queue", "events", "--follow",
                          "--for", reader, "--poll", "0.1", "--no-code-watch"], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        cursor = Path(cfg["data_dir"]) / "agentic" / "cursors" / f"{reader}.json"
        assert waits.until(cursor.exists)
        time.sleep(1.0)                          # several loops; a negative check
        assert _watchers(f.pid) == []
        assert not AW.log_path(cfg).exists() and not AW.state_path(cfg).exists()
        f.send_signal(signal.SIGINT)
        out, _ = f.communicate(timeout=60)
        assert out == b"" and f.returncode == 0
    finally:
        _kill(f)

