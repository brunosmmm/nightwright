"""SPEC-0499: the suite must not spawn real or fake `notify-send` on the desktop."""
import gc
import inspect
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

from nightwright.agentic import escalations as ESC

pytestmark = pytest.mark.fast

_IMPORT_TIME_POPEN = subprocess.Popen
_IMPORT_TIME_RUN = subprocess.run


class _SubPopen(subprocess.Popen):
    pass


@pytest.fixture
def fake_notify_send(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    marker = tmp_path / "notify-send-ran.marker"
    script = bindir / "notify-send"
    script.write_text(f"#!/bin/sh\n touch '{marker}'\n exit 0\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    return script, marker


def _assert_which_is_fake(fake_script):
    assert shutil.which("notify-send") == str(fake_script)


def test_notify_auto_does_not_spawn_notify_send(fake_notify_send):
    fake_script, marker = fake_notify_send
    _assert_which_is_fake(fake_script)
    cfg = {"data_dir": "/tmp/unused"}
    rec = {"id": "ESC-0001", "question": "ship it?"}
    out = ESC.notify(cfg, rec)
    assert out["via"] == "notify-send" and out["ok"] is True
    assert not marker.exists()


def test_apply_escalate_in_tmp_store_does_not_spawn_notify_send(tmp_path, fake_notify_send):
    fake_script, marker = fake_notify_send
    _assert_which_is_fake(fake_script)
    cfg = {"data_dir": str(tmp_path / "data")}
    plan = ESC.plan_escalate(cfg, None, question="runner broken?", options=["wait", "restart"],
                             recommend=1, topic="scope")
    rec = ESC.apply_escalate(cfg, plan)
    assert rec["id"] == "ESC-0001"
    assert ESC.read(cfg, "ESC-0001") is not None
    assert not marker.exists()


def test_subprocess_notify_send_fails_closed(fake_notify_send):
    fake_script, marker = fake_notify_send
    _assert_which_is_fake(fake_script)
    fake = str(fake_script)
    with pytest.raises(AssertionError, match="notify-send"):
        subprocess.run([fake, "t", "b"])
    assert not marker.exists()
    with pytest.raises(AssertionError, match="notify-send"):
        subprocess.Popen([fake, "t", "b"])
    assert not marker.exists()
    with pytest.raises(AssertionError, match="notify-send"):
        subprocess.run("notify-send t b", shell=True)
    assert not marker.exists()


def _blocked(fn, marker, *a, **kw):
    with pytest.raises(AssertionError, match="notify-send"):
        fn(*a, **kw)
    assert not marker.exists()


def test_guard_patches_popen_init_only():
    assert subprocess.Popen is _IMPORT_TIME_POPEN
    assert subprocess.run is _IMPORT_TIME_RUN


@pytest.mark.parametrize("call", [subprocess.run, subprocess.Popen], ids=["run", "Popen"])
@pytest.mark.parametrize("argv", [["notify-send", "t", "b"], ("notify-send", "t", "b")],
                         ids=["list", "tuple"])
def test_subprocess_notify_send_bare_name_fails_closed(fake_notify_send, call, argv):
    fake_script, marker = fake_notify_send
    _assert_which_is_fake(fake_script)
    _blocked(call, marker, argv)
    _blocked(call, marker, tuple(argv) if isinstance(argv, list) else list(argv))
    _blocked(call, marker, [str(fake_script), "t", "b"])


def test_notify_send_non_str_argv_fails_closed(fake_notify_send):
    fake_script, marker = fake_notify_send
    _assert_which_is_fake(fake_script)
    fake = str(fake_script)
    _blocked(subprocess.run, marker, os.fsencode(fake))
    _blocked(subprocess.run, marker, bytearray(os.fsencode(fake)))
    _blocked(subprocess.run, marker, pathlib.Path(fake))
    _blocked(subprocess.run, marker, [os.fsencode(fake), b"t"])
    _blocked(subprocess.run, marker, [pathlib.Path(fake), "t"])
    _blocked(subprocess.run, marker, b"notify-send t b", shell=True)
    _blocked(subprocess.run, marker, b"sh -c 'notify-send t'", shell=True)


@pytest.mark.parametrize("cmd", [
    "sh -c \"notify-send t b\"",
    "bash -lc 'notify-send t'",
    "env X=1 notify-send t b",
    "true && notify-send t b",
    "timeout 5 notify-send t b",
    "timeout 5s notify-send t b",
    "timeout -s KILL 5 notify-send t b",
    "nice -n 5 notify-send t b",
    "sudo -u root notify-send t b",
    "if true; then notify-send t; fi",
    "sh -c \"sh -c 'notify-send t'\"",
    "true;notify-send t",
    "true&&notify-send t",
    "echo hi\nnotify-send t",
    "/tmp/a+b/notify-send t",
    "X=a:b notify-send t",
    "true&&notify-send \"x",
    "(notify-send t)",
])
def test_notify_send_behind_a_shell_fails_closed(fake_notify_send, cmd):
    fake_script, marker = fake_notify_send
    _assert_which_is_fake(fake_script)
    _blocked(subprocess.run, marker, cmd, shell=True)


def test_notify_send_shell_wrapper_as_list_fails_closed(fake_notify_send):
    fake_script, marker = fake_notify_send
    _assert_which_is_fake(fake_script)
    _blocked(subprocess.run, marker, ["sh", "-c", "notify-send t b"])
    _blocked(subprocess.run, marker, ["bash", "-lc", "notify-send t"])
    _blocked(subprocess.run, marker, ["env", "X=1", "notify-send", "t", "b"])


def test_notify_send_mentions_do_not_trip_the_guard(fake_notify_send):
    fake_script, marker = fake_notify_send
    _assert_which_is_fake(fake_script)
    for argv in (["echo", "notify-send"], [sys.executable, "-c", "pass", "notify-send"]):
        assert subprocess.run(argv, capture_output=True).returncode == 0
    assert subprocess.run("echo notify-send", shell=True, capture_output=True).returncode == 0
    subprocess.run(["which", "notify-send"], capture_output=True)  # exit status not asserted
    assert not marker.exists()


def test_import_time_popen_bindings_fail_closed(fake_notify_send):
    from nightwright.agentic import gates
    fake_script, marker = fake_notify_send
    _assert_which_is_fake(fake_script)
    for cls in (_IMPORT_TIME_POPEN, _SubPopen, gates._MeteredPopen):
        _blocked(cls, marker, ["notify-send", "t", "b"])


def test_production_popen_defaults_fail_closed(fake_notify_send):
    from nightwright.agentic import herdr_autowatch, queue, restart
    fake_script, marker = fake_notify_send
    _assert_which_is_fake(fake_script)
    for fn in (queue.detach_runner, restart.apply_restart, herdr_autowatch.Supervisor.__init__):
        popen = inspect.signature(fn).parameters["popen"].default
        assert popen is _IMPORT_TIME_POPEN
        _blocked(popen, marker, ["notify-send", "t", "b"])


def _build_blocked_popen():
    try:
        subprocess.Popen(["notify-send", "t"])
    except AssertionError:
        return True
    return False


def test_blocked_popen_leaves_no_unraisable_del_exception(monkeypatch):
    seen = []
    monkeypatch.setattr(sys, "unraisablehook", seen.append)
    assert _build_blocked_popen()
    gc.collect()
    assert not seen


def test_other_subprocess_commands_still_run():
    r = subprocess.run([sys.executable, "-c", "print(1)"], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout == "1\n"


def test_popen_of_other_commands_stays_a_popen():
    proc = subprocess.Popen([sys.executable, "-c", "print(1)"], stdout=subprocess.PIPE, text=True)
    assert isinstance(proc, subprocess.Popen)
    out, _ = proc.communicate()
    assert out == "1\n"


def test_module_stub_still_observes_argv(fake_notify_send, monkeypatch):
    fake_script, marker = fake_notify_send
    _assert_which_is_fake(fake_script)
    seen = []

    class _Done:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(ESC, "_run_notify", lambda argv: seen.append(list(argv)) or _Done())
    cfg = {"data_dir": "/tmp/unused"}
    rec = {"id": "ESC-0001", "question": "q?"}
    ESC.notify(cfg, rec)
    assert os.path.basename(seen[0][0]) == "notify-send"
    assert not marker.exists()
