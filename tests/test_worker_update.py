"""SPEC-0369: `nw worker update` (the worker side of a self-update without root), offline. The
installer is a fake `uv` earlier on PATH and the verifying `nw` a fake script, so no network and
no real install; `test_worker_update_real_uv.py` runs the real tools."""
import fcntl
import hashlib
import io
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

import pytest

from nightwright import buildid
from nightwright.agentic import worker as W
from nightwright.agentic import worker_job as WJ
from nightwright.agentic import worker_update as WU
from test_worker import _gate, _header
from waits import until

pytestmark = pytest.mark.fast

WHEEL = "nightwright-0.1.0-py3-none-any.whl"
OLD = "b1:" + "0" * 64

FAKE_UV = r"""#!/bin/sh
D="$FAKE_DIR"
echo "$@" > "$D/uv.argv"
id -u > "$D/uv.uid"
grep '^SigIgn' /proc/$$/status | cut -f2 > "$D/uv.sigign"
cut -d' ' -f6 /proc/$$/stat > "$D/uv.sid"
mode=ok; [ -f "$D/mode" ] && mode=$(cat "$D/mode")
case "$mode" in
  fail) echo "resolving..." >&2; echo "error: boom" >&2; echo >&2; exit 2;;
  slow) sleep 30; exit 0;;
  block) touch "$D/started"
         while [ ! -f "$D/release" ]; do sleep 0.05; done
         echo "still going after the close" >&2;;
esac
[ -f "$4" ] || { echo "error: no wheel at $4" >&2; exit 9; }
cat "$D/will_install" > "$D/installed"
"""
FAKE_NW = r"""#!/bin/sh
echo $$ > "$FAKE_DIR/nw.pid"
[ -f "$FAKE_DIR/installed" ] || exit 1
printf '{"build_id": "%s", "version": "0.1.0"}\n' "$(cat "$FAKE_DIR/installed")"
"""


def _script(path: Path, text: str) -> str:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def make_wheel(body: bytes = b"x = 1\n", init=True) -> tuple[bytes, str]:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        if init:
            z.writestr("nightwright/__init__.py", "")
        z.writestr("nightwright/mod.py", body)
        z.writestr("nightwright-0.1.0.dist-info/METADATA", "Name: nightwright\n")
    data = buf.getvalue()
    p = Path(os.environ["TMPDIR_FOR_WHEEL"]) / "w.whl"
    p.write_bytes(data)
    return data, buildid.compute_wheel(p)


def stream(data: bytes, build: str, **over) -> io.BytesIO:
    h = {"schema": WU.SCHEMA, "wheel": WHEEL, "size": len(data),
         "sha256": hashlib.sha256(data).hexdigest(), "build_id": build, **over}
    return io.BytesIO(json.dumps(h).encode() + b"\n" + data)


class Env:
    def __init__(self, tmp: Path, monkeypatch):
        self.dir = tmp / "fake"
        self.dir.mkdir()
        bins = tmp / "bin"
        bins.mkdir()
        _script(bins / "uv", FAKE_UV)
        self.nw = _script(bins / "nw", FAKE_NW)
        monkeypatch.setenv("FAKE_DIR", str(self.dir))
        monkeypatch.setenv("TMPDIR_FOR_WHEEL", str(tmp))
        monkeypatch.setenv("PATH", f"{bins}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setattr(buildid, "local", self.local)
        self.cfg = {"data_dir": str(tmp / "wdata"), "config_dir": str(tmp / "wconf")}
        (self.dir / "installed").write_text(OLD)

    def local(self):
        f = self.dir / "installed"
        return f.read_text().strip() if f.exists() else OLD

    def will_install(self, build):
        (self.dir / "will_install").write_text(build)

    def mode(self, m):
        (self.dir / "mode").write_text(m)

    @property
    def wd(self):
        return W.worker_dir(self.cfg)

    def run(self, data, build, *, force=False, **over):
        out = io.BytesIO()
        code = WU.update(self.cfg, force, stream(data, build, **over), out, nw_argv=[self.nw])
        lines = out.getvalue().decode().splitlines()
        assert len(lines) == 1
        return code, json.loads(lines[0])

    def uv_ran(self):
        return (self.dir / "uv.argv").exists()


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


@pytest.fixture
def new(env):
    data, build = make_wheel()
    env.will_install(build)
    return data, build


def test_install_updates_as_the_invoking_user(env, new):
    data, build = new
    code, res = env.run(data, build)
    assert code == 0 and res["state"] == "updated" and res["schema"] == WU.RESULT_SCHEMA
    assert res["build_id_before"] == OLD and res["build_id_after"] == build
    assert res["version"] == "0.1.0" and res["forced"] is False and res["error"] is None
    argv = (env.dir / "uv.argv").read_text().split()
    assert argv[:3] == ["tool", "install", "--reinstall"]
    assert argv[3].endswith(WHEEL) and len(argv) == 4
    assert (env.dir / "uv.uid").read_text().strip() == str(os.getuid())
    d = WU.update_dir(env.cfg)
    assert not list(d.glob("*.whl")) and not list(d.glob("*.part")) and (d / "uv.log").exists()
    assert not (env.wd / "updating").exists()
    saved = json.loads((env.wd / "update.json").read_text())
    assert saved["state"] == "updated" and saved["install_started_at"] > 0
    assert saved["install_finished_at"] >= saved["install_started_at"]
    assert oct(d.stat().st_mode & 0o777) == "0o700"


@pytest.mark.parametrize("force", [False, True])
def test_unchanged_does_not_run_uv(env, new, force):
    data, build = new
    (env.dir / "installed").write_text(build)
    code, res = env.run(data, build, force=force)
    assert code == 0 and res["state"] == "unchanged" and res["build_id_after"] == build
    assert not env.uv_ran() and not (env.wd / "updating").exists()


@pytest.mark.parametrize("over", [
    {"schema": "nw.worker.update.v0"}, {"wheel": "../nightwright-0.1.0-py3-none-any.whl"},
    {"wheel": "evil-0.1.0-py3-none-any.whl"}, {"wheel": "nightwright-0.1.0.tar.gz"},
    {"wheel": "nightwright-0.1.0-py3-none-any.whl\n"}, {"wheel": 5},
    {"size": 0}, {"size": WU.MAX_WHEEL_BYTES + 1}, {"size": True}, {"size": "9"},
    {"sha256": "abc"}, {"sha256": "A" * 64}, {"build_id": "b2:" + "0" * 64},
    {"build_id": "b1:" + "0" * 63}, {"build_id": None},
])
def test_bad_header_writes_nothing(env, new, over):
    data, build = new
    code, res = env.run(data, build, **over)
    assert code == 3 and res["state"] == "failed" and res["stage"] == "header"
    assert not WU.update_dir(env.cfg).exists() and not env.uv_ran()


@pytest.mark.parametrize("line", [b"", b"not json\n", b"[]\n", b"{" + b"x" * 70000 + b"\n"])
def test_unreadable_header(env, line):
    out = io.BytesIO()
    assert WU.update(env.cfg, False, io.BytesIO(line), out, nw_argv=[env.nw]) == 3
    assert json.loads(out.getvalue())["stage"] == "header"
    assert not WU.update_dir(env.cfg).exists()


def test_truncated_upload_and_extra_bytes(env, new):
    data, build = new
    s = stream(data, build)
    full = s.getvalue()
    out = io.BytesIO()
    assert WU.update(env.cfg, False, io.BytesIO(full[:-5]), out, nw_argv=[env.nw]) == 3
    res = json.loads(out.getvalue())
    assert res["stage"] == "receive" and "short" in res["error"] and not env.uv_ran()
    assert not list(WU.update_dir(env.cfg).glob("*.part"))
    # bytes past `size` are ignored
    code, res = env.run(data + b"trailing junk", build, size=len(data),
                        sha256=hashlib.sha256(data).hexdigest())
    assert code == 0 and res["state"] == "updated"


def test_bad_sha(env, new):
    data, build = new
    code, res = env.run(data, build, sha256="0" * 64)
    assert (code, res["stage"]) == (3, "receive") and not env.uv_ran()
    assert not list(WU.update_dir(env.cfg).glob("*.part"))


def test_corrupt_zip_missing_init_and_build_mismatch(env, new):
    data, build = new
    bad = b"PK\x03\x04 this is not a zip " * 4
    for blob, b in [(bad, build), (*make_wheel(init=False),), (data, "b1:" + "1" * 64)]:
        code, res = env.run(blob, b)
        assert (code, res["state"], res["stage"]) == (3, "failed", "validate"), res
        assert not env.uv_ran() and env.local() == OLD
    # a flipped byte inside a member fails testzip()
    flipped = bytearray(data)
    i = flipped.index(b"x = 1")
    flipped[i] ^= 0xFF
    code, res = env.run(bytes(flipped), build)
    assert (code, res["stage"]) == (3, "validate") and not env.uv_ran()


# ---- live jobs -------------------------------------------------------------------------------

def hold_job(env, job_id, state="running"):
    job = W.jobs_dir(env.cfg) / job_id
    job.mkdir(parents=True)
    (job / "job.json").write_text(json.dumps({"job_id": job_id, "state": state}))
    fd = os.open(job / "job.lock", os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


@pytest.mark.parametrize("state", ["running", "accepted"])
def test_live_job_is_busy_unless_forced(env, new, state):
    data, build = new
    fd = hold_job(env, "j1", state)
    try:
        code, res = env.run(data, build)
        assert (code, res["state"], res["live_jobs"]) == (5, "busy", ["j1"])
        assert not env.uv_ran() and env.local() == OLD and not (env.wd / "updating").exists()
        assert not list(WU.update_dir(env.cfg).glob("*.part"))
        code, res = env.run(data, build, force=True)
        assert code == 0 and res["state"] == "updated" and res["forced"] is True
        assert res["live_jobs"] == ["j1"]
    finally:
        os.close(fd)


HELPER = r"""
import fcntl, os, signal, sys, time
lock, hit = sys.argv[1], sys.argv[2]
def note(sig, frame):
    open(hit, "a").write(str(sig) + "\n")
for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT, signal.SIGUSR1, signal.SIGUSR2):
    signal.signal(sig, note)
fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX)
print("ready", flush=True)
time.sleep(60)
"""


def test_force_installs_without_signalling_a_live_job_process(env, new, tmp_path):
    data, build = new
    job = W.jobs_dir(env.cfg) / "jp"
    job.mkdir(parents=True)
    (job / "job.json").write_text(json.dumps({"job_id": "jp", "state": "running"}))
    hit = tmp_path / "signals"
    p = subprocess.Popen([sys.executable, "-I", "-c", HELPER, str(job / "job.lock"), str(hit)],
                         stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "ready"
        code, res = env.run(data, build)
        assert (code, res["state"], res["live_jobs"]) == (5, "busy", ["jp"])
        code, res = env.run(data, build, force=True)
        assert (code, res["state"], res["live_jobs"]) == (0, "updated", ["jp"])
        assert p.poll() is None and not hit.exists()        # still running, never signalled
    finally:
        p.kill()
        p.wait()


def test_a_lost_job_is_not_live(env, new):
    data, build = new
    job = W.jobs_dir(env.cfg) / "gone"
    job.mkdir(parents=True)
    (job / "job.json").write_text(json.dumps({"job_id": "gone", "state": "lost"}))
    (job / "job.lock").touch()
    code, res = env.run(data, build)
    assert code == 0 and res["state"] == "updated" and res["live_jobs"] == []


# ---- concurrency -----------------------------------------------------------------------------

class Running:
    """An update whose fake uv blocks until released, in a thread."""

    def __init__(self, env, data, build):
        self.env = env
        env.mode("block")
        self.result = {}

        def go():
            self.result["r"] = env.run(data, build)
        self.t = threading.Thread(target=go)
        self.t.start()
        assert until(lambda: (env.dir / "started").exists())

    def finish(self):
        (self.env.dir / "release").touch()
        self.t.join(30)
        assert not self.t.is_alive()
        return self.result["r"]


def claim_header(job_id):
    return _header(job_id, "a" * 40, "b" * 40, [_gate("g", "true")], bundle_bytes=0)


def line(h):
    return json.dumps(h).encode() + b"\n"


def test_second_update_is_busy_and_a_claim_is_refused_during_one(env, new):
    data, build = new
    run = Running(env, data, build)
    try:
        assert (env.wd / "updating").exists()
        code, res = env.run(data, build)
        assert (code, res["state"], res["stage"]) == (5, "busy", "lock")
        assert res["error"] == "another update is running"
        assert (env.wd / "updating").exists()           # the running one's marker is untouched
        d = WU.update_dir(env.cfg)                      # ... and so are its staged files
        assert (d / WHEEL).is_file() and not list(d.glob("*.part"))
        # jobs_lock is free while uv runs
        assert W.gc(env.cfg)["errors"] == []
        with W.jobs_lock(env.cfg):
            pass
        # a gate-job claim
        out = io.BytesIO()
        assert W.gate_job(env.cfg, io.BytesIO(line(claim_header("g1"))), out) == W.EXIT_ERROR
        err = json.loads(out.getvalue().splitlines()[-1])
        assert (err["type"], err["stage"], err["error"], err["retryable"]) == \
            ("error", "claim", "worker updating", True)
        # a submit claim
        out = io.BytesIO()
        assert WJ.submit(env.cfg, io.BytesIO(line(claim_header("s1"))), out) == W.EXIT_ERROR
        err = json.loads(out.getvalue().splitlines()[-1])
        assert (err["stage"], err["error"], err["retryable"]) == ("claim", "worker updating", True)
        assert not (W.jobs_dir(env.cfg) / "g1").exists()
        assert not (W.jobs_dir(env.cfg) / "s1").exists()
    finally:
        code, res = run.finish()
    assert code == 0 and res["state"] == "updated"


def test_a_marker_left_by_a_killed_update_does_not_refuse(env):
    env.wd.mkdir(parents=True)
    (env.wd / "updating").write_text("{}")
    with W.jobs_lock(env.cfg):
        assert W.update_refusal(env.cfg) is None
    assert not (env.wd / "updating").exists()


def test_a_claimant_older_than_the_install_is_refused(env, monkeypatch):
    env.wd.mkdir(parents=True)
    (env.wd / "update.json").write_text(json.dumps({"install_started_at": 2000.0}))
    monkeypatch.setattr(W, "PROCESS_STARTED", 1000.0)
    out = io.BytesIO()
    assert W.gate_job(env.cfg, io.BytesIO(line(claim_header("g2"))), out) == W.EXIT_ERROR
    err = json.loads(out.getvalue().splitlines()[-1])
    assert (err["error"], err["retryable"]) == ("worker updating", True)
    out = io.BytesIO()
    assert WJ.submit(env.cfg, io.BytesIO(line(claim_header("s2"))), out) == W.EXIT_ERROR
    assert json.loads(out.getvalue().splitlines()[-1])["retryable"] is True
    assert not W.jobs_dir(env.cfg).exists() or not list(W.jobs_dir(env.cfg).iterdir())
    monkeypatch.setattr(W, "PROCESS_STARTED", 3000.0)       # a process started after it
    with W.jobs_lock(env.cfg):
        assert W.update_refusal(env.cfg) is None


def test_a_claimant_started_during_the_install_is_refused_after_it(env, monkeypatch):
    env.wd.mkdir(parents=True)
    rec = env.wd / "update.json"
    monkeypatch.setattr(W, "PROCESS_STARTED", 3000.0)       # between start and finish
    rec.write_text(json.dumps({"install_started_at": 2000.0, "install_finished_at": 4000.0}))
    with W.jobs_lock(env.cfg):
        assert W.update_refusal(env.cfg) == W.UPDATING_ERROR
    out = io.BytesIO()
    assert W.gate_job(env.cfg, io.BytesIO(line(claim_header("g3"))), out) == W.EXIT_ERROR
    assert json.loads(out.getvalue().splitlines()[-1])["error"] == "worker updating"
    monkeypatch.setattr(W, "PROCESS_STARTED", 5000.0)       # after the install ended
    with W.jobs_lock(env.cfg):
        assert W.update_refusal(env.cfg) is None
    # no finish time (an install still running, or killed): the start is the bound
    monkeypatch.setattr(W, "PROCESS_STARTED", 3000.0)
    rec.write_text(json.dumps({"install_started_at": 2000.0, "state": "installing"}))
    with W.jobs_lock(env.cfg):
        assert W.update_refusal(env.cfg) is None


# ---- failures --------------------------------------------------------------------------------

def test_uv_failure_reports_its_last_line(env, new, capsys):
    data, build = new
    env.mode("fail")
    code, res = env.run(data, build)
    assert (code, res["state"], res["stage"]) == (3, "failed", "install")
    assert res["error"] == "error: boom"
    assert "resolving..." in capsys.readouterr().err        # the log tail goes to stderr
    assert json.loads((env.wd / "update.json").read_text())["state"] == "failed"
    assert not (env.wd / "updating").exists()


def test_uv_timeout(env, new, monkeypatch):
    data, build = new
    env.mode("slow")
    monkeypatch.setattr(WU, "INSTALL_TIMEOUT_S", 0.5)
    code, res = env.run(data, build)
    assert (code, res["stage"]) == (3, "install") and "timed out" in res["error"]


def test_stderr_closed_does_not_replace_the_answer(env, new, monkeypatch):
    data, build = new
    env.mode("fail")
    closed = io.StringIO()
    closed.close()
    monkeypatch.setattr(sys, "stderr", closed)
    code, res = env.run(data, build)
    assert (code, res["stage"], res["error"]) == (3, "install", "error: boom")


def test_an_unexpected_error_names_the_stage_that_was_running(env, new, monkeypatch):
    data, build = new

    def boom(*a, **k):
        raise KeyError("x")
    monkeypatch.setattr(WU, "_receive", boom)
    code, res = env.run(data, build)
    assert (code, res["state"], res["stage"]) == (3, "failed", "receive")
    monkeypatch.undo()
    monkeypatch.setenv("FAKE_DIR", str(env.dir))
    monkeypatch.setenv("TMPDIR_FOR_WHEEL", str(env.dir.parent))
    monkeypatch.setattr(WU, "_validate", boom)
    code, res = env.run(data, build)
    assert (code, res["stage"]) == (3, "validate")


def test_stale_wheels_are_removed_before_a_receive(env, new):
    data, build = new
    d = WU.update_dir(env.cfg)
    d.mkdir(parents=True)
    (d / "nightwright-0.0.1-py3-none-any.whl").write_bytes(b"old")
    (d / "x.part").write_bytes(b"old")
    code, res = env.run(data, build, sha256="0" * 64)       # fails after the cleanup
    assert (code, res["stage"]) == (3, "receive")
    assert not list(d.glob("*.whl")) and not list(d.glob("*.part"))


# ---- finding uv ------------------------------------------------------------------------------

@pytest.fixture
def no_uv(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sys, "executable", str(tmp_path / "py" / "python"))
    monkeypatch.setattr(WU, "UV_FALLBACKS", ("~/.local/bin/uv", str(tmp_path / "nope" / "uv")))
    return tmp_path


def test_find_uv_next_to_the_running_nw(no_uv):
    nwbin = no_uv / "nwbin"
    nwbin.mkdir()
    uv = _script(nwbin / "uv", "#!/bin/sh\n")
    assert WU.find_uv([str(nwbin / "nw")]) == uv


def test_find_uv_in_the_home_fallback(no_uv):
    (no_uv / "home" / ".local" / "bin").mkdir(parents=True)
    uv = _script(no_uv / "home" / ".local" / "bin" / "uv", "#!/bin/sh\n")
    assert WU.find_uv([str(no_uv / "elsewhere" / "nw")]) == uv


def test_find_uv_prefers_path_and_can_find_nothing(no_uv, monkeypatch):
    assert WU.find_uv([str(no_uv / "elsewhere" / "nw")]) is None
    onpath = no_uv / "onpath"
    onpath.mkdir()
    uv = _script(onpath / "uv", "#!/bin/sh\n")
    monkeypatch.setenv("PATH", str(onpath))
    assert WU.find_uv([str(no_uv / "elsewhere" / "nw")]) == uv


def test_uv_missing(env, new, monkeypatch):
    data, build = new
    monkeypatch.setattr(WU, "find_uv", lambda nw: None)
    code, res = env.run(data, build)
    assert (code, res["stage"], res["error"]) == (3, "install", "uv not found for this user")


def test_wrong_build_after_install_fails_verify(env, new):
    data, build = new
    env.will_install("b1:" + "9" * 64)
    code, res = env.run(data, build)
    assert (code, res["stage"], res["build_id_after"]) == (3, "verify", "b1:" + "9" * 64)
    assert json.loads((env.wd / "update.json").read_text())["state"] == "failed"


def test_verify_runs_in_a_new_process(env, new):
    data, build = new
    code, _ = env.run(data, build)
    assert code == 0 and int((env.dir / "nw.pid").read_text()) != os.getpid()


def test_verify_when_nw_does_not_run(env, new):
    data, build = new
    (env.dir / "installed").unlink()            # the fake nw exits 1 until `installed` exists
    _script(Path(env.nw).parent / "uv", "#!/bin/sh\nexit 0\n")      # uv installs nothing
    code, res = env.run(data, build)
    assert (code, res["stage"]) == (3, "verify") and "did not answer" in res["error"]


# ---- a dropped connection --------------------------------------------------------------------

DRIVER = r"""
import os, sys
from nightwright import buildid
from nightwright.agentic import worker_update as WU
buildid.local = lambda: open(os.environ["FAKE_DIR"] + "/installed").read().strip()
cfg = {"data_dir": os.environ["NW_DATA_DIR"], "config_dir": os.environ["NW_DATA_DIR"]}
sys.exit(WU.update(cfg, False, sys.stdin.buffer, sys.stdout.buffer, nw_argv=[os.environ["FAKE_NW"]]))
"""


def test_a_dropped_connection_does_not_abort_the_install(env, new, tmp_path):
    data, build = new
    env.mode("block")
    penv = {**os.environ, "NW_DATA_DIR": env.cfg["data_dir"], "FAKE_NW": env.nw}
    p = subprocess.Popen([sys.executable, "-I", "-c", DRIVER], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=penv,
                         cwd=tmp_path)
    p.stdin.write(stream(data, build).getvalue())
    p.stdin.close()
    assert until(lambda: (env.dir / "started").exists())
    updater_sid = os.getsid(p.pid)
    p.stdout.close()
    p.stderr.close()
    os.kill(p.pid, signal.SIGHUP)
    (env.dir / "release").touch()
    # 120: the interpreter's own exit-time flush of the closed stdout failed
    assert p.wait(30) in (0, 120)
    wd = W.worker_dir({"data_dir": env.cfg["data_dir"], "config_dir": env.cfg["data_dir"]})
    saved = json.loads((wd / "update.json").read_text())
    assert saved["state"] == "updated" and env.local() == build
    mask = int((env.dir / "uv.sigign").read_text().strip(), 16)
    for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM, signal.SIGPIPE):
        assert not mask & (1 << (sig - 1)), sig.name       # default dispositions, not ignored
    assert int((env.dir / "uv.sid").read_text()) != updater_sid
    log = (WU.update_dir({"data_dir": env.cfg["data_dir"], "config_dir": ""}) / "uv.log")
    assert "still going after the close" in log.read_text()

